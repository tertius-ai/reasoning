// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
// The server stores at most 64,000 UTF-8 bytes of uploader-extracted PDF text.
export const PDF_TEXT_BYTES = 60000;

type TextItem = { str?: string } | object;
export type PdfTextSource = {
  numPages: number;
  getPage(number: number): Promise<{ getTextContent(): Promise<{ items: TextItem[] }> }>;
};

/** Text the preview endpoint accepts: no NUL or unpaired surrogates, at most `maxBytes` UTF-8 bytes. */
export function storablePreview(text: string, maxBytes = PDF_TEXT_BYTES): string {
  const clean = text.replace(/\u0000/g, " ")
    .replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g, "�");
  const encoded = new TextEncoder().encode(clean);
  if (encoded.length <= maxBytes) return clean;
  const prefix = new TextDecoder().decode(encoded.slice(0, maxBytes));
  return prefix.endsWith("�") && !clean.startsWith(prefix) ? prefix.slice(0, -1) : prefix;
}

/** Page-marked text until the byte allowance is used; a later failure keeps the pages already read. */
export async function collectPdfText(pdf: PdfTextSource, maxBytes = PDF_TEXT_BYTES): Promise<string> {
  const encoder = new TextEncoder();
  let text = "", bytes = 0;
  try {
    for (let number = 1; number <= pdf.numPages && bytes < maxBytes; number++) {
      const content = await (await pdf.getPage(number)).getTextContent();
      const words = content.items.flatMap(item => "str" in item && typeof item.str === "string" ? [item.str] : []);
      const page = `\n[Page ${number}]\n` + words.join(" ");
      text += page;
      bytes += encoder.encode(page).length;
    }
  } catch {
    // A timeout or damaged page ends extraction; earlier pages remain useful.
  }
  return storablePreview(text, maxBytes);
}
