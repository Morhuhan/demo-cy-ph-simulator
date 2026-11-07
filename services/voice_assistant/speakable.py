"""Текст для синтезатора: числа словами, аббревиатуры по буквам.

Silero цифр не читает — пропускает их или произносит невнятно, а ответы
наставника пишет языковая модель, и числа в них бывают. Аббревиатуры
регламента синтезатор читает как слово: «ЛВЖ» превращается в нечто
непроизносимое.
"""

from __future__ import annotations

import re

_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["ноль", "одна", "две"] + _UNITS[3:]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]

#: Как читать сокращения, которые встречаются в регламентах и ответах модели.
ABBREVIATIONS = {
    "ЛВЖ": "эл вэ жэ",
    "ГЖ": "гэ жэ",
    "СИЗ": "сиз",
    "СОП": "соп",
    "КФТ": "ка эф тэ",
    "СУГ": "суг",
    "УСН": "у эс эн",
    "в/ц": "вагон-цистерна",
    "т.е.": "то есть",
    "т.д.": "так далее",
    "°C": " градусов",
    "°С": " градусов",
    "%": " процентов",
}


def _triad(n: int, feminine: bool) -> list[str]:
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if 10 <= n < 20:
        words.append(_TEENS[n - 10])
    else:
        if n >= 20:
            words.append(_TENS[n // 10])
            n %= 10
        if n:
            words.append((_UNITS_F if feminine else _UNITS)[n])
    return words


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 10 <= n % 100 < 20:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def number_words(n: int) -> str:
    """Целое число словами, мужской род: 1205 → «одна тысяча двести пять»."""
    if n == 0:
        return "ноль"
    if n < 0:
        return "минус " + number_words(-n)
    if n >= 1_000_000_000:
        return " ".join(_UNITS[int(d)] for d in str(n))
    words = []
    millions, rest = divmod(n, 1_000_000)
    thousands, units = divmod(rest, 1000)
    if millions:
        words += _triad(millions, False) + [_plural(millions, "миллион", "миллиона", "миллионов")]
    if thousands:
        words += _triad(thousands, True) + [_plural(thousands, "тысяча", "тысячи", "тысяч")]
    words += _triad(units, False)
    return " ".join(words)


def _number(match: re.Match) -> str:
    sign, whole, frac = match.group(1), match.group(2), match.group(3)
    text = number_words(int(whole.replace(" ", "")))
    if frac:
        text += " и " + number_words(int(frac)) + " " + {1: "десятых", 2: "сотых"}.get(len(frac), "")
    return ("минус " if sign == "-" else "") + text.strip()


def speakable(text: str) -> str:
    text = " ".join(text.split())
    for short, full in ABBREVIATIONS.items():
        text = re.sub(rf"(?<![\w]){re.escape(short)}(?![\w])" if short[0].isalpha() else re.escape(short), full, text)
    # «10–20» — диапазон (тире читается паузой), «-40» в начале фразы или
    # после пробела — минус.
    text = re.sub(r"(\d)\s*[–—-]\s*(\d)", r"\1 – \2", text)
    text = re.sub(r"(?:(?<=\s)|^)\+(?=\d)", "плюс ", text)
    text = re.sub(r"(?:(?<=\s)|^)(-)?(\d{1,3}(?: \d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)", _number, text)
    text = re.sub(r"(?<!\d)(\d+)", lambda m: number_words(int(m.group(1))), text)
    return " ".join(text.split())
