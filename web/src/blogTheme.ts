import { initializeColorMode } from "./appearance";

// Every server-rendered blog page applies the reader's explicit light/dark choice. Math enhancement is separate and
// is included only on pages that show math.
if (document.body.hasAttribute("data-research-blog")) initializeColorMode();
