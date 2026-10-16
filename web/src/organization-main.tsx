import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "katex/dist/katex.min.css";
import "./styles.css";
import "./organization.css";
import { OrganizationShell } from "./OrganizationShell";
import { applyAppearance, readAppearance, initializeColorMode } from "./appearance";

applyAppearance(readAppearance());
initializeColorMode();
createRoot(document.getElementById("root")!).render(<React.StrictMode><OrganizationShell /></React.StrictMode>);
