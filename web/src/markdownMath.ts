/** A delimiter counts only when its backslash is not itself escaped, so a LaTeX row break with spacing such as
 * `\\[2mm]` is `\\` followed by `[2mm]`, not the start of display math. Matching `\\` first keeps pairs together. */
const DELIMITER = /\\\\|\\([()[\]])/g;

/** Convert conventional LaTeX delimiters to the dollar delimiters understood
 * by remark-math, without changing inline or fenced code. */
export function normalizeMathDelimiters(text: string): string {
  const parts = text.split(/(```[\s\S]*?```|`[^`]*`)/g);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) return part;
      return part.replace(DELIMITER, (match, bracket: string | undefined) =>
        bracket === undefined ? match : bracket === "(" || bracket === ")" ? "$" : "\n$$\n");
    })
    .join("");
}
