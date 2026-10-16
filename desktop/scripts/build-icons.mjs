import { execFileSync } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { app, BrowserWindow } from "electron";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const assets = path.join(desktopRoot, "assets");
const svgPath = path.join(assets, "icon.svg");
const pngPath = path.join(assets, "icon.png");
const icnsPath = path.join(assets, "icon.icns");
const icoPath = path.join(assets, "icon.ico");
const icoSizes = [16, 24, 32, 48, 64, 128, 256];

async function renderSvg(sizes) {
  const svgUrl = `data:image/svg+xml;base64,${readFileSync(svgPath).toString("base64")}`;
  const window = new BrowserWindow({
    width: 1024,
    height: 1024,
    show: false,
    transparent: true,
    backgroundColor: "#00000000",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });
  await window.loadURL("data:text/html,<html><body></body></html>");
  // Each size is rasterized from the SVG directly so small icons stay crisp.
  const rendered = await window.webContents.executeJavaScript(`
    new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        resolve(${JSON.stringify(sizes)}.map((size) => {
          const canvas = document.createElement("canvas");
          canvas.width = size;
          canvas.height = size;
          const context = canvas.getContext("2d");
          context.drawImage(image, 0, 0, size, size);
          return {
            size,
            dataUrl: canvas.toDataURL("image/png"),
            cornerAlpha: context.getImageData(0, 0, 1, 1).data[3],
          };
        }));
      };
      image.onerror = () => reject(new Error("Could not render icon.svg"));
      image.src = ${JSON.stringify(svgUrl)};
    })
  `);
  window.destroy();
  const pngs = new Map();
  for (const { size, dataUrl, cornerAlpha } of rendered) {
    if (cornerAlpha !== 0) {
      throw new Error(`Generated ${size}px icon does not have transparent corners`);
    }
    pngs.set(size, Buffer.from(dataUrl.split(",", 2)[1], "base64"));
  }
  return pngs;
}

function renderIco(pngs) {
  // ICO container of PNG-encoded images; a width/height byte of 0 means 256.
  const header = Buffer.alloc(6);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(pngs.length, 4);
  const entries = Buffer.alloc(16 * pngs.length);
  let offset = header.length + entries.length;
  pngs.forEach(([size, png], index) => {
    const entry = index * 16;
    entries.writeUInt8(size % 256, entry);
    entries.writeUInt8(size % 256, entry + 1);
    entries.writeUInt16LE(1, entry + 4);
    entries.writeUInt16LE(32, entry + 6);
    entries.writeUInt32LE(png.length, entry + 8);
    entries.writeUInt32LE(offset, entry + 12);
    offset += png.length;
  });
  writeFileSync(icoPath, Buffer.concat([header, entries, ...pngs.map(([, png]) => png)]));
}

function renderIcns() {
  if (process.platform !== "darwin") {
    // ic10 is the standard 1024px PNG icon representation. This also keeps
    // the macOS resource synchronized when contributors regenerate on Linux.
    const png = readFileSync(pngPath);
    const header = Buffer.alloc(16);
    header.write("icns", 0); header.writeUInt32BE(png.length + 16, 4);
    header.write("ic10", 8); header.writeUInt32BE(png.length + 8, 12);
    writeFileSync(icnsPath, Buffer.concat([header, png]));
    return;
  }
  const temporaryRoot = mkdtempSync(path.join(os.tmpdir(), "reasoning-icon-"));
  const iconset = path.join(temporaryRoot, "icon.iconset");
  try {
    mkdirSync(iconset);
    const outputs = [
      [16, "icon_16x16.png"],
      [32, "icon_16x16@2x.png"],
      [32, "icon_32x32.png"],
      [64, "icon_32x32@2x.png"],
      [128, "icon_128x128.png"],
      [256, "icon_128x128@2x.png"],
      [256, "icon_256x256.png"],
      [512, "icon_256x256@2x.png"],
      [512, "icon_512x512.png"],
      [1024, "icon_512x512@2x.png"],
    ];
    for (const [size, name] of outputs) {
      execFileSync("sips", ["-z", String(size), String(size), pngPath, "--out", path.join(iconset, name)], {
        stdio: "ignore",
      });
    }
    execFileSync("iconutil", ["-c", "icns", iconset, "-o", icnsPath]);
  } finally {
    rmSync(temporaryRoot, { force: true, recursive: true });
  }
}

app.whenReady().then(async () => {
  try {
    const pngs = await renderSvg([1024, ...icoSizes]);
    writeFileSync(pngPath, pngs.get(1024));
    renderIcns();
    renderIco(icoSizes.map((size) => [size, pngs.get(size)]));
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    app.quit();
  }
});
