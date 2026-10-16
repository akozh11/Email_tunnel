import re
import time
from google import genai
from google.genai import types
from settings import GEMINI_API_KEY

client = genai.Client(api_key=GEMINI_API_KEY)

# Просить ли модель сразу писать без LaTeX (страховка latex_to_text остаётся в любом случае)
NO_LATEX_HINT = True

_SYSTEM_PROMPT = (
    "Отвечай обычным текстом без LaTeX и без разметки формул. "
    "Формулы записывай в строку Unicode-символами: x², √2, a/b, ≤, ≠, π, ∑, ∫."
)

_GENERATION_CONFIG = (
    types.GenerateContentConfig(system_instruction=_SYSTEM_PROMPT) if NO_LATEX_HINT else None
)


# ======================================================================
#  LaTeX -> обычный текст
# ======================================================================

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

_SYMBOLS = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "ast": "*", "star": "⋆",
    "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗", "setminus": "∖",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "sim": "~", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫",
    "lt": "<", "gt": ">", "perp": "⊥", "parallel": "∥", "mid": "|", "colon": ":",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "mapsto": "↦", "longrightarrow": "→",
    "longleftarrow": "←", "implies": "⇒", "iff": "⇔", "uparrow": "↑", "downarrow": "↓",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧",
    "lor": "∨", "vee": "∨", "angle": "∠", "triangle": "△", "therefore": "∴", "because": "∵",
    "infty": "∞", "partial": "∂", "nabla": "∇", "hbar": "ℏ", "ell": "ℓ", "aleph": "ℵ",
    "Re": "ℜ", "Im": "ℑ", "degree": "°", "prime": "′", "dagger": "†",
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
    "bigcup": "⋃", "bigcap": "⋂",
    "ldots": "…", "dots": "…", "cdots": "…", "vdots": "⋮", "ddots": "⋱",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "lbrace": "{", "rbrace": "}", "quad": " ", "qquad": "  ",
}

# Операторы и отношения, вокруг которых ставятся пробелы
_SPACED = {
    "cdot", "times", "div", "pm", "mp", "oplus", "otimes", "setminus",
    "leq", "le", "geq", "ge", "neq", "ne", "approx", "equiv", "sim", "simeq", "cong",
    "propto", "ll", "gg", "perp", "parallel",
    "to", "rightarrow", "leftarrow", "Rightarrow", "Leftarrow", "leftrightarrow",
    "Leftrightarrow", "mapsto", "longrightarrow", "longleftarrow", "implies", "iff",
    "in", "notin", "ni", "subset", "subseteq", "supset", "supseteq", "cup", "cap",
    "land", "lor", "wedge", "vee",
}

_FUNCS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "arccot",
    "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "lim", "limsup", "liminf",
    "max", "min", "sup", "inf", "det", "dim", "ker", "gcd", "deg", "arg", "Pr",
}

# Команды, которые оставляют только содержимое аргумента
_PASS_CMDS = {
    "textbf", "textit", "textsf", "texttt", "emph", "mathrm", "mathbf", "mathit", "mathsf",
    "mathtt", "mathcal", "mathscr", "mathfrak", "boldsymbol", "bm", "operatorname",
    "boxed", "cancel", "underline", "underbrace", "overbrace",
}
# Команды, содержимое которых — обычный текст (без математической обработки)
_RAW_CMDS = {"text", "textrm", "textnormal", "mbox", "hbox"}
# Команды, аргумент которых отбрасывается
_DROP_ARG_CMDS = {"label", "phantom", "vphantom", "hphantom", "color"}
# Команды, которые просто пропускаются
_SKIP_CMDS = {
    "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
    "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
    "limits", "nolimits", "hline", "nonumber", "notag", "mathstrut", "relax",
}

_ACCENTS = {
    "hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303", "widetilde": "\u0303",
    "bar": "\u0304", "overline": "\u0305", "vec": "\u20d7", "overrightarrow": "\u20d7",
    "dot": "\u0307", "ddot": "\u0308", "acute": "\u0301", "grave": "\u0300",
    "check": "\u030c", "breve": "\u0306",
}

_BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "H": "ℍ"}

_ESCAPED = {
    ",": " ", ";": " ", ":": " ", "!": "", " ": " ", ">": " ",
    "{": "{", "}": "}", "%": "%", "&": "&", "_": "_", "#": "#", "$": "$", "|": "‖",
}

_SUP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷",
    "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ",
    "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ",
    "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ",
    "z": "ᶻ",
    "A": "ᴬ", "B": "ᴮ", "D": "ᴰ", "E": "ᴱ", "G": "ᴳ", "H": "ᴴ", "I": "ᴵ", "J": "ᴶ",
    "K": "ᴷ", "L": "ᴸ", "M": "ᴹ", "N": "ᴺ", "O": "ᴼ", "P": "ᴾ", "R": "ᴿ", "T": "ᵀ",
    "U": "ᵁ", "V": "ⱽ", "W": "ᵂ",
}

_SUB = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇",
    "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ",
    "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ",
    "x": "ₓ",
}

# Знаки, которые в верхнем индексе остаются как есть (x′, 90°)
_SUP_AS_IS = {"°", "′", "″", "*", "∗", "†", "‡"}

# Вне формул (текст без $...$) заменяем только однозначные команды
_TEXT_MODE_OK = set(_GREEK) | set(_SYMBOLS) | {"frac", "dfrac", "tfrac", "sqrt", "binom"}

_CMD_RE = re.compile(r"\\([A-Za-z]+)\*?|\\(.)", re.S)
_ENV_RE = re.compile(r"\\begin\{([A-Za-z]+\*?)\}(.*?)\\end\{\1\}", re.S)
_INLINE_RE = re.compile(r"(?<![\\$])\$(?![\s$])([^$\n]+?)(?<![\s\\])\$(?![\d$])")

_MATRIX_BRACKETS = {
    "matrix": ("[", "]"), "smallmatrix": ("[", "]"), "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"), "Bmatrix": ("\\{", "\\}"), "vmatrix": ("|", "|"),
    "Vmatrix": ("‖", "‖"),
}


def _find_group(s: str, i: int):
    """s[i] == '{'. Возвращает (содержимое, индекс после закрывающей скобки) или None."""
    if i >= len(s) or s[i] != "{":
        return None
    depth = 0
    j = i
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None


def _read_arg(s: str, i: int):
    """Читает аргумент команды: {группа}, \\команда или один символ."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return None
    if s[i] == "{":
        return _find_group(s, i)
    if s[i] == "\\":
        m = _CMD_RE.match(s, i)
        return m.group(0), m.end()
    return s[i], i + 1


def _wrap(x: str) -> str:
    """Берёт выражение в скобки, если оно не простое (для дробей и корней)."""
    x = x.strip()
    if not x:
        return x
    if re.fullmatch(r"-?[\w.,′°]+", x):
        return x
    if x[0] == "(" and x[-1] == ")":
        depth = 0
        for k, ch in enumerate(x):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and k < len(x) - 1:
                    break
        else:
            return x
    return f"({x})"


def _script(content: str, sup: bool, raw: str = "") -> str:
    """Верхний/нижний индекс: Unicode, если всё конвертируется, иначе ^(...) / _(...)."""
    c = content.strip()
    if not c:
        return ""
    if sup and raw.strip() == "\\circ":
        return "°"
    if sup and c in _SUP_AS_IS:
        return c
    table = _SUP if sup else _SUB
    if all(ch in table for ch in c):
        return "".join(table[ch] for ch in c)
    op = "^" if sup else "_"
    if re.fullmatch(r"\w+", c):
        return op + c
    return f"{op}({c})"


def _convert_env(m) -> str:
    """\\begin{...}...\\end{...}: матрицы, cases, aligned и т.п."""
    env = m.group(1).rstrip("*")
    body = _ENV_RE.sub(_convert_env, m.group(2))
    body = body.replace("\\hline", "")
    if env == "array":
        body = re.sub(r"^\s*\{[^{}]*\}", "", body)  # описание столбцов {cc}
    rows = [r.strip() for r in re.split(r"\\\\(?:\s*\[[^\]]*\])?", body)]
    rows = [r for r in rows if r]

    if env in _MATRIX_BRACKETS:
        left, right = _MATRIX_BRACKETS[env]
        lines = [", ".join(c.strip() for c in r.split("&")) for r in rows]
        return left + "; ".join(lines) + right
    if env == "cases":
        lines = [r.replace("&", ", ") for r in rows]
        return "\n".join(("\\{ " if k == 0 else "   ") + ln for k, ln in enumerate(lines))
    # aligned, align, gather, equation, split, eqnarray, array и прочее
    return "\n".join(r.replace("&", " ") for r in rows)


def _handle_command(name: str, s: str, i: int, math: bool, literal: str):
    """Обрабатывает команду \\name. Возвращает (новый индекс, результат)."""
    if not math and name not in _TEXT_MODE_OK:
        return i, literal

    if name in _SKIP_CMDS:
        if name in ("left", "right") and s[i:i + 1] == ".":
            i += 1
        return i, ""

    if name in _GREEK:
        return i, _GREEK[name]

    if name in _SYMBOLS:
        sym = _SYMBOLS[name]
        return i, (f" {sym} " if math and name in _SPACED else sym)

    if name in _FUNCS:
        return i, name + (" " if s[i:i + 1] == "\\" else "")

    if name in ("frac", "dfrac", "tfrac", "cfrac"):
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        b = _read_arg(s, a[1])
        if b is None:
            return a[1], _conv(a[0])
        num = _conv(a[0]).strip()
        den = _conv(b[0]).strip()
        return b[1], f"{_wrap(num)}/{_wrap(den)}"

    if name == "binom":
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        b = _read_arg(s, a[1])
        if b is None:
            return a[1], _conv(a[0])
        return b[1], f"C({_conv(a[0]).strip()}, {_conv(b[0]).strip()})"

    if name == "sqrt":
        index = None
        mm = re.match(r"\s*\[([^\]]*)\]", s[i:])
        if mm:
            index = _conv(mm.group(1)).strip()
            i += mm.end()
        a = _read_arg(s, i)
        if a is None:
            return i, "√"
        body = _wrap(_conv(a[0]))
        if index in (None, "", "2"):
            prefix = "√"
        elif index == "3":
            prefix = "∛"
        elif index == "4":
            prefix = "∜"
        else:
            prefix = _script(index, True, index) + "√"
        return a[1], prefix + body

    if name == "mathbb":
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        return a[1], "".join(_BLACKBOARD.get(ch, ch) for ch in _conv(a[0]))

    if name in _ACCENTS:
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        c = _conv(a[0]).strip()
        mark = _ACCENTS[name]
        if len(c) == 1:
            res = c + mark
        elif name in ("overline", "bar") and c.isalnum():
            res = "".join(ch + mark for ch in c)
        elif name in ("vec", "overrightarrow"):
            res = "→" + c
        else:
            res = c
        return a[1], res

    if name in _RAW_CMDS:
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        return a[1], a[0]

    if name in _PASS_CMDS:
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        return a[1], _conv(a[0], math)

    if name in _DROP_ARG_CMDS:
        a = _read_arg(s, i)
        return (a[1] if a else i), ""

    if name == "textcolor":
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        b = _read_arg(s, a[1])
        if b is None:
            return a[1], ""
        return b[1], _conv(b[0], math)

    if name == "tag":
        a = _read_arg(s, i)
        if a is None:
            return i, ""
        return a[1], f"({a[0]})"

    # Неизвестная команда внутри формулы — оставляем само слово без обратного слэша
    return i, name


def _conv(s: str, math: bool = True) -> str:
    """
    Основной проход по строке.
    math=True  — режим формулы (обрабатываются ^, _, {}, &, ~ и все известные команды);
    math=False — обычный текст: заменяются только однозначные команды (\\alpha, \\to, \\frac...).
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]

        if ch == "\\":
            m = _CMD_RE.match(s, i)
            if m is None:
                out.append(ch)
                i += 1
                continue
            name, sym = m.group(1), m.group(2)
            i = m.end()
            if name is None:  # \\, \{, \, и подобные
                if not math:
                    out.append("\\" + sym)
                elif sym == "\\":
                    mm = re.match(r"\s*\[[^\]]*\]", s[i:])
                    if mm:
                        i += mm.end()
                    out.append("\n")
                else:
                    out.append(_ESCAPED.get(sym, sym))
                continue
            i, piece = _handle_command(name, s, i, math, m.group(0))
            out.append(piece)
            continue

        if math:
            if ch in "^_":
                arg = _read_arg(s, i + 1)
                if arg is None:
                    i += 1
                    continue
                raw, i = arg
                out.append(_script(_conv(raw), ch == "^", raw))
                continue
            if ch == "{":
                group = _find_group(s, i)
                if group:
                    out.append(_conv(group[0]))
                    i = group[1]
                else:
                    i += 1
                continue
            if ch == "}":
                i += 1
                continue
            if ch in "&~":
                out.append(" ")
                i += 1
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def _math_to_text(s: str) -> str:
    """Содержимое формулы -> обычный текст."""
    s = _ENV_RE.sub(_convert_env, s)
    s = _conv(s, math=True)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"([(\[])[ \t]+", r"\1", s)
    s = re.sub(r"[ \t]+([)\],.;])", r"\1", s)
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
    return s.strip()


def _inline_math(m) -> str:
    content = m.group(1)
    if re.fullmatch(r"[\d\s.,]+", content):  # "$5 ... 6$" — это деньги, а не формула
        return m.group(0)
    return _math_to_text(content)


def latex_to_text(text: str) -> str:
    """
    Переводит LaTeX-разметку в обычный читаемый текст.

    Обрабатывает: $...$, $$...$$, \\(...\\), \\[...\\], окружения (matrix, cases, aligned...),
    дроби, корни, степени и индексы, греческие буквы, математические символы.
    Код (``` ... ``` и `...`) не трогается.

    Пример: "$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$"  ->  "x = (-b ± √(b² - 4ac))/2a"
    """
    if not text or ("\\" not in text and "$" not in text):
        return text

    # Прячем блоки кода, чтобы не испортить $ и \ внутри них
    stash = []

    def _hide(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```.*?```|`[^`\n]*`", _hide, text, flags=re.S)

    text = re.sub(r"\$\$(.+?)\$\$", lambda m: "\n" + _math_to_text(m.group(1)) + "\n", text, flags=re.S)
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: "\n" + _math_to_text(m.group(1)) + "\n", text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: _math_to_text(m.group(1)), text, flags=re.S)
    text = _INLINE_RE.sub(_inline_math, text)
    text = _ENV_RE.sub(lambda m: _math_to_text(m.group(0)), text)

    # Команды без $...$ (\rightarrow, \times, \frac{a}{b} прямо в тексте)
    if "\\" in text:
        text = _conv(text, math=False)

    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ======================================================================
#  Запросы к Gemini
# ======================================================================

def _generate_with_retry(model: str, contents: list, max_retries: int = 3, delay: int = 5) -> str:
    """Отправляет запрос в Gemini с повторными попытками при временной недоступности."""
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=_GENERATION_CONFIG
            )
            text = (response.text or "").strip()
            if not text:
                return "Gemini вернул пустой ответ (возможно, сработал фильтр безопасности)."
            return latex_to_text(text)
        except Exception as e:
            last_error = e
            error_text = str(e)

            # Повторяем только при временных ошибках (503, перегрузка)
            if "503" in error_text or "UNAVAILABLE" in error_text:
                print(f"Gemini перегружен, попытка {attempt}/{max_retries}, жду {delay} сек...")
                time.sleep(delay)
                continue
            else:
                # Другие ошибки — не повторяем, сразу возвращаем
                break

    return f"Ошибка при обращении к Gemini после {max_retries} попыток: {last_error}"


def ask_gemini_with_image(text: str, image_bytes: bytes = None, mime_type: str = "image/jpeg",
                           model: str = "gemini-3.5-flash-lite") -> str:
    contents = []
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
    contents.append(text if text else "Опиши, что на изображении.")

    return _generate_with_retry(model, contents)


def ask_gemini_with_images(text: str, images: list, model: str = "gemini-3.5-flash-lite") -> str:
    contents = [text if text else "Опиши, что на изображениях."]
    for img in images:
        contents.append(types.Part.from_bytes(data=img["data"], mime_type=img["mime_type"]))

    return _generate_with_retry(model, contents)

