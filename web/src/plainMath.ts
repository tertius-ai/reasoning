// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
/**
 * TeX math as one line of plain Unicode text, for places that quote a message in a single line of text, such as reply
 * pointers and titles, where KaTeX cannot be used: `\chi\big(\mathrm{Gr}(2,5)\big)` reads `χ(Gr(2,5))` and
 * `\sigma_1 \cdot \sigma_\lambda` reads `σ₁ · σ_λ`. Common symbols become their Unicode characters, font commands keep
 * their argument (`\mathbb{Z}` is `ℤ`), sizing and spacing commands are dropped, and any other command keeps its name, as
 * an operator name such as `\Hom` or `\sin` would read. A subscript or superscript uses Unicode subscript or superscript
 * characters when every character has one, and is otherwise written after `_` or `^`, in parentheses when longer than one
 * character. The result is meant to be read, not rendered back to TeX.
 */

const SYMBOLS: Record<string, string> = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ϵ", varepsilon: "ε", zeta: "ζ", eta: "η", theta: "θ",
  vartheta: "ϑ", iota: "ι", kappa: "κ", varkappa: "ϰ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", omicron: "ο", pi: "π",
  varpi: "ϖ", rho: "ρ", varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ", upsilon: "υ", phi: "ϕ", varphi: "φ",
  chi: "χ", psi: "ψ", omega: "ω", digamma: "ϝ",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ",
  Omega: "Ω",
  cdot: "·", cdotp: "·", times: "×", div: "÷", pm: "±", mp: "∓", ast: "*", star: "⋆", circ: "∘", bullet: "•",
  otimes: "⊗", oplus: "⊕", ominus: "⊖", odot: "⊙", wedge: "∧", land: "∧", vee: "∨", lor: "∨", neg: "¬", lnot: "¬",
  cap: "∩", cup: "∪", sqcup: "⊔", sqcap: "⊓", setminus: "∖", smallsetminus: "∖", amalg: "⨿", wr: "≀", dagger: "†",
  ddagger: "‡",
  le: "≤", leq: "≤", leqslant: "≤", ge: "≥", geq: "≥", geqslant: "≥", ne: "≠", neq: "≠", ll: "≪", gg: "≫",
  approx: "≈", equiv: "≡", cong: "≅", sim: "∼", simeq: "≃", propto: "∝", prec: "≺", succ: "≻", preceq: "⪯",
  succeq: "⪰", coloneqq: "≔", coloneq: "≔", doteq: "≐", triangleq: "≜",
  in: "∈", notin: "∉", ni: "∋", subset: "⊂", subseteq: "⊆", subsetneq: "⊊", supset: "⊃", supseteq: "⊇",
  supsetneq: "⊋", mid: "∣", nmid: "∤", parallel: "∥", perp: "⊥", vdash: "⊢", models: "⊨", top: "⊤", bot: "⊥",
  lhd: "◁", rhd: "▷", triangleleft: "◁", triangleright: "▷", unlhd: "⊴", unrhd: "⊵", trianglelefteq: "⊴",
  to: "→", rightarrow: "→", longrightarrow: "⟶", leftarrow: "←", gets: "←", longleftarrow: "⟵", leftrightarrow: "↔",
  mapsto: "↦", longmapsto: "⟼", Rightarrow: "⇒", Longrightarrow: "⟹", implies: "⟹", Leftarrow: "⇐",
  Longleftarrow: "⟸", impliedby: "⟸", Leftrightarrow: "⇔", iff: "⟺", hookrightarrow: "↪", hookleftarrow: "↩",
  twoheadrightarrow: "↠", rightrightarrows: "⇉", leftrightarrows: "⇆", rightleftharpoons: "⇌", uparrow: "↑",
  downarrow: "↓", nearrow: "↗", searrow: "↘", leadsto: "⇝", rightsquigarrow: "⇝",
  sum: "∑", prod: "∏", coprod: "∐", int: "∫", iint: "∬", iiint: "∭", oint: "∮", bigcup: "⋃", bigcap: "⋂",
  bigsqcup: "⨆", bigoplus: "⨁", bigotimes: "⨂", bigwedge: "⋀", bigvee: "⋁",
  infty: "∞", partial: "∂", nabla: "∇", forall: "∀", exists: "∃", nexists: "∄", emptyset: "∅", varnothing: "∅",
  aleph: "ℵ", beth: "ℶ", hbar: "ℏ", ell: "ℓ", wp: "℘", Re: "ℜ", Im: "ℑ", prime: "′", angle: "∠", triangle: "△",
  square: "□", Box: "□", blacksquare: "■", checkmark: "✓", sharp: "♯", flat: "♭", natural: "♮", surd: "√",
  ldots: "…", dots: "…", dotsc: "…", dotsb: "⋯", cdots: "⋯", vdots: "⋮", ddots: "⋱",
  langle: "⟨", rangle: "⟩", lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉", lvert: "|", rvert: "|", vert: "|",
  lVert: "‖", rVert: "‖", Vert: "‖", lbrace: "{", rbrace: "}", lbrack: "[", rbrack: "]", backslash: "\\",
  colon: ":", bmod: "mod", mod: "mod",
  "|": "‖", "{": "{", "}": "}", "%": "%", "&": "&", "#": "#", "_": "_", "$": "$",
};

/** Commands that only size, space or label, dropped with nothing in their place. */
const DROPPED = new Set(["big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr",
  "bigm", "Bigm", "left", "right", "middle", "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle", "limits",
  "nolimits", "nonumber", "notag", "!", "negthinspace", "allowbreak", "nobreak", "relax"]);
const SPACES = new Set([",", ";", ":", ">", " ", "quad", "qquad", "enspace", "thinspace", "medspace", "thickspace"]);
/** Commands whose one argument is dropped with them. */
const SKIPPED = new Set(["label", "tag", "hspace", "vspace", "phantom", "hphantom", "vphantom", "color"]);
/** Commands that keep their last argument as it is: fonts, boxes and decorations without a Unicode form. */
const PLAIN = new Set(["mathrm", "mathit", "mathbf", "mathsf", "mathtt", "mathnormal", "boldsymbol", "bm", "pmb",
  "mathsfit", "boxed", "cancel", "bcancel", "xcancel", "underbrace", "overbrace", "smash", "mathop", "mathrel", "mathbin",
  "mathord", "mathopen", "mathclose", "mathpunct", "mathinner"]);
const TEXT = new Set(["text", "textrm", "textbf", "textit", "textsf", "texttt", "textup", "textnormal", "textsc", "emph",
  "mbox", "hbox", "textmd", "textsl"]);
/** Combining accents: `\hat{x}` is `x̂`. */
const ACCENTS: Record<string, string> = { hat: "̂", widehat: "̂", tilde: "̃", widetilde: "̃",
  bar: "̄", overline: "̅", vec: "⃗", overrightarrow: "⃗", dot: "̇", ddot: "̈",
  check: "̌", widecheck: "̌", breve: "̆", acute: "́", grave: "̀", mathring: "̊",
  underline: "̲" };
const ARROWS: Record<string, string> = { xrightarrow: "→", xleftarrow: "←", xhookrightarrow: "↪", xhookleftarrow: "↩",
  xtwoheadrightarrow: "↠", xmapsto: "↦", xRightarrow: "⇒", xLeftarrow: "⇐", xleftrightarrow: "↔", xLeftrightarrow: "⇔" };
const BRACKETS: Record<string, [string, string]> = { pmatrix: ["(", ")"], bmatrix: ["[", "]"], Bmatrix: ["{", "}"],
  vmatrix: ["|", "|"], Vmatrix: ["‖", "‖"], cases: ["{ ", ""], dcases: ["{ ", ""], rcases: ["", " }"] };

const SUBSCRIPTS = zip("0123456789+-−=()aehijklmnoprstuvxβγρφϕχ", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵩᵪ");
const SUPERSCRIPTS = zip("0123456789+-−=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVWαβγδθφϕχ",
  "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂᵅᵝᵞᵟᶿᵠᵠᵡ");
/** Superscripts that read as they are, without `^`: `f^\prime` is `f′`, `90^\circ` is `90°`. */
const RAISED: Record<string, string> = { "′": "′", "″": "″", "*": "*", "∗": "*", "†": "†", "‡": "‡", "∘": "°" };

function zip(from: string, to: string): Map<string, string> {
  const a = [...from], b = [...to];
  return new Map(a.map((c, i) => [c, b[i]]));
}

/** Letters in a Unicode mathematical alphabet, with the letters that Unicode encodes elsewhere. */
function alphabet(upper: number, lower: number, holes: Record<string, string>, digits?: number): (text: string) => string {
  return text => [...text].map(c => holes[c] ?? (c >= "A" && c <= "Z" ? String.fromCodePoint(upper + c.charCodeAt(0) - 65)
    : c >= "a" && c <= "z" ? String.fromCodePoint(lower + c.charCodeAt(0) - 97)
      : digits && c >= "0" && c <= "9" ? String.fromCodePoint(digits + c.charCodeAt(0) - 48) : c)).join("");
}
const doubleStruck = alphabet(0x1d538, 0x1d552, { C: "ℂ", H: "ℍ", N: "ℕ", P: "ℙ", Q: "ℚ", R: "ℝ", Z: "ℤ" }, 0x1d7d8);
const script = alphabet(0x1d49c, 0x1d4b6, { B: "ℬ", E: "ℰ", F: "ℱ", H: "ℋ", I: "ℐ", L: "ℒ", M: "ℳ", R: "ℛ", e: "ℯ",
  g: "ℊ", o: "ℴ" });
const fraktur = alphabet(0x1d504, 0x1d51e, { C: "ℭ", H: "ℌ", I: "ℑ", R: "ℜ", Z: "ℨ" });
const ALPHABETS: Record<string, (text: string) => string> = { mathbb: doubleStruck, Bbb: doubleStruck,
  mathbbm: doubleStruck, mathds: doubleStruck, mathcal: script, mathscr: script, mathfrak: fraktur, frak: fraktur };

/** A subscript or superscript in Unicode characters if each has one; else after `_` or `^`, in parentheses if long. */
function scripted(text: string, superscript: boolean): string {
  const compact = text.replace(/\s+/g, "");
  if (!compact) return "";
  if (superscript && [...compact].every(c => RAISED[c])) return [...compact].map(c => RAISED[c]).join("");
  const map = superscript ? SUPERSCRIPTS : SUBSCRIPTS;
  const chars = [...compact];
  if (chars.every(c => map.has(c))) return chars.map(c => map.get(c)).join("");
  return (superscript ? "^" : "_") + (chars.length === 1 || /^\(.*\)$/.test(compact) ? compact : "(" + compact + ")");
}

/** `text` in parentheses unless it is one term: a fraction's numerator or denominator, a root's radicand. */
const term = (text: string) => {
  const compact = text.trim();
  return /^[\p{L}\p{N}\p{M}′.]+$/u.test(compact) || /^\([^()]*\)$/.test(compact) ? compact : "(" + compact + ")";
};

/** The math in `tex`, without its delimiters, as plain text on one line. */
export function plainMath(tex: string): string {
  let i = 0;
  /** Whether the text so far ends in an operator name, which a following letter must not run into. */
  let word: boolean = false;

  const space = () => { while (i < tex.length && /\s/.test(tex[i])) i++; };
  const commandName = () => {
    const letters = /^[A-Za-z]+/.exec(tex.slice(i));
    const name = letters ? letters[0] : tex[i] ?? "";
    i += name.length;
    return name;
  };
  /** The raw text of a braced group starting at `i`, or of the next character. */
  const rawGroup = (): string => {
    space();
    if (tex[i] !== "{") {
      if (tex[i] === "\\") { i++; return "\\" + commandName(); }
      return tex[i++] ?? "";
    }
    let depth = 0;
    const start = i + 1;
    for (; i < tex.length; i++) {
      if (tex[i] === "\\") { i++; continue; }
      if (tex[i] === "{") depth++;
      else if (tex[i] === "}" && --depth === 0) return tex.slice(start, i++);
    }
    return tex.slice(start);
  };
  const optional = (): string | null => {
    space();
    if (tex[i] !== "[") return null;
    const end = tex.indexOf("]", i);
    if (end < 0) return null;
    const inner = tex.slice(i + 1, end);
    i = end + 1;
    return inner;
  };
  /** One argument of a command, or of `_` and `^`, converted. */
  const argument = (): string => {
    space();
    if (tex[i] === "{") return plainMath(rawGroup());
    if (tex[i] === "\\") { i++; return command(commandName()); }
    return character(tex[i++] ?? "");
  };
  const character = (c: string) => c === "'" ? "′" : c === "~" ? " " : c === "&" ? " " : c;
  const text = (raw: string) => raw.split(/(\$[^$]*\$)/).map((part, index) => index % 2 ? plainMath(part.slice(1, -1))
    : part.replace(/\\([&%$#_{} ])/g, "$1").replace(/\\[A-Za-z]+\s*|[{}]/g, "").replace(/~/g, " ")).join("");

  const command = (name: string): string => {
    if (name === "\\") { optional(); return "; "; }
    if (Object.hasOwn(SYMBOLS, name)) return SYMBOLS[name];
    if (DROPPED.has(name)) {
      // `\left.` and `\right.` size an empty delimiter.
      space();
      if (tex[i] === ".") i++;
      return "";
    }
    if (SPACES.has(name)) return " ";
    if (SKIPPED.has(name)) { rawGroup(); return ""; }
    if (PLAIN.has(name)) return argument();
    if (TEXT.has(name)) return text(rawGroup());
    if (Object.hasOwn(ALPHABETS, name)) return ALPHABETS[name](argument());
    if (Object.hasOwn(ACCENTS, name)) {
      const base = argument(), chars = [...base], mark = ACCENTS[name];
      if (chars.length === 1) return base + mark;
      // A long bar or underline marks every character; other wide accents have no plain form.
      return mark === "̅" || mark === "̄" || mark === "̲"
        ? chars.map(c => c + (mark === "̲" ? mark : "̅")).join("") : base;
    }
    if (Object.hasOwn(ARROWS, name)) {
      optional();
      const label = argument().trim();
      return label ? "-" + label + ARROWS[name] : ARROWS[name];
    }
    switch (name) {
      case "frac": case "dfrac": case "tfrac": case "cfrac": {
        const numerator = argument(), denominator = argument();
        return term(numerator) + "/" + term(denominator);
      }
      case "binom": case "dbinom": case "tbinom": {
        const n = argument(), k = argument();
        return "C(" + n.trim() + "," + k.trim() + ")";
      }
      case "sqrt": {
        const index = optional(), radicand = term(argument());
        if (!index?.trim()) return "√" + radicand;
        const degree = plainMath(index).trim();
        return (degree === "3" ? "∛" : degree === "4" ? "∜" : scripted(degree, true) + "√") + radicand;
      }
      case "pmod": return "(mod " + argument().trim() + ")";
      case "operatorname": { if (tex[i] === "*") i++; word = true; return plainMath(rawGroup()); }
      case "not": {
        const negated = argument();
        return negated === "=" ? "≠" : negated === "∈" ? "∉" : negated === "∃" ? "∄" : negated + "̸";
      }
      case "overset": case "underset": case "stackrel": argument(); return argument();
      case "textcolor": case "colorbox": rawGroup(); return argument();
      case "newcommand": case "renewcommand": case "providecommand":
        if (tex[i] === "*") i++;
        rawGroup(); optional(); optional(); rawGroup(); return "";
      case "def": case "gdef":
        space();
        if (tex[i] === "\\") { i++; commandName(); }
        while (i < tex.length && tex[i] !== "{") i++;
        rawGroup();
        return "";
      case "begin": {
        const environment = rawGroup().replace(/\*$/, "");
        // Column specifications are not content.
        if (["array", "subarray", "alignat", "alignedat"].includes(environment)) rawGroup();
        return Object.hasOwn(BRACKETS, environment) ? BRACKETS[environment][0] : "";
      }
      case "end": {
        const environment = rawGroup().replace(/\*$/, "");
        return Object.hasOwn(BRACKETS, environment) ? BRACKETS[environment][1] : "";
      }
    }
    // Any other command, such as `\sin`, `\Hom` or a macro, reads as its name.
    word = /^[A-Za-z]+$/.test(name);
    return name;
  };

  let out = "";
  /** Adds `piece`; a letter or digit right after an operator name gets a space, so `\sin\theta` reads `sin θ`. */
  const append = (piece: string, name = false) => {
    if (word && /^[\p{L}\p{N}]/u.test(piece)) out += " ";
    out += piece;
    word = name;
  };
  while (i < tex.length) {
    const c = tex[i++];
    if (c === "\\") {
      const after: boolean = word;
      word = false;
      const piece = command(commandName()), name = word;
      word = after;
      if (piece) append(piece, name);
    } else if (c === "_" || c === "^") {
      // A script stays with an operator name: `\log_2 n` reads `log₂ n`, `\sin^2\theta` reads `sin² θ`.
      const after: boolean = word, piece = scripted(argument(), c === "^");
      out += piece;
      word = after;
    } else if (c === "{" || c === "}") {
      continue;
    } else if (/\s/.test(c)) {
      if (!out.endsWith(" ")) out += " ";
      word = false;
    } else {
      append(character(c));
    }
  }
  return out.replace(/\s+/g, " ").replace(/([([{⟨]) /g, "$1").replace(/ ([)\]}⟩;])/g, "$1")
    .replace(/(?:\s*;)+\s*(?=[)\]}|‖]|$)/g, "").trim();
}
