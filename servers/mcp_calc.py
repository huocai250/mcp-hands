"""MCP server: everyday calculations - safe expression evaluation, units, dates, stats.

Never uses eval(): expressions go through ast.parse and a whitelist of node types,
and money/percent work stays on decimal.Decimal so 0.1 + 0.2 prints 0.3.
"""
import ast
import datetime
import decimal
import math
import operator
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

D = decimal.Decimal
CTX = decimal.Context(prec=40, traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])
MAX_INPUT = 20000
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
LOCAL_ZONE = "local"


# --------------------------------------------------------------- number helpers
def _dec(value):
    """Coerce to Decimal without float noise (floats go through their repr)."""
    if isinstance(value, D):
        return value
    if isinstance(value, bool):
        return D(int(value))
    if isinstance(value, float):
        return D(repr(value))
    return D(str(value).strip().replace("_", "").replace(",", ""))


def _clean(text):
    """Decimal -> short string: no exponent, no trailing zeros, no '-0'."""
    t = str(text)
    if "E" in t or "e" in t:
        t = format(text.normalize(), "f")
    if "." in t:
        t = t.rstrip("0").rstrip(".") or "0"
    if t in ("-0", "+0"):
        t = "0"
    if set(t) <= set("-0."):
        t = "0"
    return t


def _round(value, digits, mode="half_up"):
    return _dec(value).quantize(D(1).scaleb(-int(digits)), rounding=_MODE[mode])


def _digits(precision):
    try:
        return max(0, min(int(precision), 30))
    except (TypeError, ValueError):
        return 10


# ------------------------------------------------------------------ expression
BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _sqrt(x):
    if x < 0:
        raise ValueError("sqrt of a negative number is not real: %s" % _clean(x))
    return CTX.sqrt(x)


def _ln(x):
    if x <= 0:
        raise ValueError("ln needs a positive argument, got %s" % _clean(x))
    return CTX.ln(x)


def _log(x):
    if x <= 0:
        raise ValueError("log needs a positive argument, got %s" % _clean(x))
    return CTX.log10(x)


FUNCS = {
    "sqrt": _sqrt,
    "sin": lambda x: D(repr(math.sin(float(x)))),
    "cos": lambda x: D(repr(math.cos(float(x)))),
    "tan": lambda x: D(repr(math.tan(float(x)))),
    "log": _log,
    "ln": _ln,
    "abs": abs,
    "round": lambda x: CTX.to_integral_value(x),
    "floor": lambda x: D(math.floor(float(x))),
    "ceil": lambda x: D(math.ceil(float(x))),
    "exp": lambda x: D(repr(math.exp(float(x)))),
}


def _power(base, exponent):
    if exponent == exponent.to_integral_value() and abs(exponent) <= 1000:
        return CTX.power(base, int(exponent))
    if base < 0:
        raise ValueError("a negative base needs a whole-number exponent")
    return D(repr(math.pow(float(base), float(exponent))))


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed in expressions")
        return _dec(node.value)
    if isinstance(node, ast.BinOp):
        op = BIN_OPS.get(type(node.op))
        if op is None:
            if isinstance(node.op, ast.Pow):
                return _power(_eval_node(node.left), _eval_node(node.right))
            raise ValueError("operator not allowed: %s" % type(node.op).__name__)
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("operator not allowed: %s" % type(node.op).__name__)
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Name):
        name = node.id
        if name in ("pi", "PI"):
            return +D(math.pi)
        if name in ("e", "E"):
            return +D(math.e)
        if name in FUNCS:
            raise ValueError("%s needs parentheses, e.g. %s(4)" % (name, name))
        raise ValueError("unknown name %r (constants: pi, e)" % name)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only plain function names can be called")
        name = node.func.id
        if name.startswith("_") or name not in FUNCS:
            raise ValueError("unknown function %r (allowed: %s)" % (name, ", ".join(sorted(FUNCS))))
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        if len(node.args) != 1:
            raise ValueError("%s takes exactly one argument" % name)
        return FUNCS[name](_eval_node(node.args[0]))
    raise ValueError("expression element not allowed: %s" % type(node).__name__)


def _evaluate(expression):
    text = str(expression or "").strip()
    if not text:
        raise ValueError("expression is empty")
    if len(text) > 500:
        raise ValueError("expression too long (500 char cap)")
    if "__" in text:
        raise ValueError("double underscore is not allowed in expressions")
    if "^" in text:
        raise ValueError("use ** for powers, not ^")
    tree = ast.parse(text, mode="eval")
    return _eval_node(tree)


# --------------------------------------------------------------------- unit data
LENGTH = {"m": "1", "km": "1000", "cm": "0.01", "mm": "0.001", "mi": "1609.344",
          "mile": "1609.344", "miles": "1609.344", "ft": "0.3048", "foot": "0.3048",
          "feet": "0.3048", "in": "0.0254", "inch": "0.0254", "inches": "0.0254",
          "yd": "0.9144", "yard": "0.9144", "nm": "1852", "nmi": "1852"}
MASS = {"kg": "1", "g": "0.001", "mg": "0.000001", "t": "1000", "ton": "1000", "tonne": "1000",
        "lb": "0.45359237", "lbs": "0.45359237", "pound": "0.45359237", "oz": "0.028349523125",
        "ounce": "0.028349523125", "jin": "0.5", "ct": "0.0002"}
AREA = {"m2": "1", "km2": "1000000", "cm2": "0.0001", "ha": "10000", "mu": "666.6666666666667",
        "ft2": "0.09290304", "in2": "0.00064516", "yd2": "0.83612736", "mi2": "2589988.110336",
        "acre": "4046.8564224"}
VOLUME = {"l": "1", "ml": "0.001", "m3": "1000", "cm3": "0.001", "gal": "3.785411784",
          "usgal": "3.785411784", "ukgal": "4.54609", "qt": "0.946352946", "pt": "0.473176473",
          "cup": "0.2365882365", "floz": "0.0295735295625"}
SPEED = {"m/s": "1", "mps": "1", "km/h": "0.2777777777777778", "kph": "0.2777777777777778",
         "mph": "0.44704", "kn": "0.5144444444444445", "knot": "0.5144444444444445",
         "ft/s": "0.3048", "fps": "0.3048"}
DATA = {"b": "1", "byte": "1", "bytes": "1", "bit": "0.125", "bits": "0.125", "kb": "1000",
        "mb": "1000000", "gb": "1000000000", "tb": "1000000000000", "pb": "1000000000000000",
        "kib": "1024", "mib": "1048576", "gib": "1073741824", "tib": "1099511627776"}
TIME = {"s": "1", "sec": "1", "second": "1", "seconds": "1", "ms": "0.001", "min": "60",
        "minute": "60", "minutes": "60", "h": "3600", "hr": "3600", "hour": "3600",
        "hours": "3600", "d": "86400", "day": "86400", "days": "86400", "w": "604800",
        "week": "604800", "weeks": "604800"}
CATEGORIES = (("length", LENGTH), ("mass", MASS), ("area", AREA), ("volume", VOLUME),
              ("speed", SPEED), ("data", DATA), ("time", TIME))
TEMP_UNITS = ("c", "celsius", "f", "fahrenheit", "k", "kelvin")


def _aliases(text):
    """Spell common unit forms the same way the tables do."""
    key = str(text or "").strip().lower().replace(" ", "")
    swaps = {"metre": "m", "meter": "m", "kilometre": "km", "kilometer": "km", "centimetre": "cm",
             "centimeter": "cm", "millimetre": "mm", "millimeter": "mm", "kilogram": "kg",
             "gram": "g", "gramme": "g", "litre": "l", "liter": "l", "millilitre": "ml",
             "milliliter": "ml", "hrs": "h", "secs": "s", "mins": "min", "kbyte": "kb",
             "mbyte": "mb", "gbyte": "gb", "tbyte": "tb", "byte": "b", "bits": "bit",
             "degc": "c", "degf": "f", "celcius": "c", "m/s2": "m/s", "kmh": "km/h",
             "kmph": "km/h", "mph": "mph", "celsius": "c", "fahrenheit": "f", "kelvin": "k",
             "squaremeter": "m2", "squaremetre": "m2", "hectare": "ha", "sqm": "m2",
             "sqft": "ft2", "gallon": "gal", "pound": "lb", "ounce": "oz", "tonne": "t"}
    return swaps.get(key, key)


def _find_category(unit):
    for name, table in CATEGORIES:
        if unit in table:
            return name, table
    return None, None


_MODE = {"half_up": decimal.ROUND_HALF_UP, "half_even": decimal.ROUND_HALF_EVEN,
         "half_down": decimal.ROUND_HALF_DOWN, "up": decimal.ROUND_UP, "down": decimal.ROUND_DOWN,
         "ceil": decimal.ROUND_CEILING, "floor": decimal.ROUND_FLOOR}


def _values(raw, cap=500):
    """Accept a list or a comma/whitespace separated string of numbers."""
    if isinstance(raw, (list, tuple)):
        parts = [str(v) for v in raw]
    else:
        parts = [p for p in str(raw or "").replace(";", ",").replace("\t", ",").split(",")]
    out = []
    for part in parts:
        text = part.strip()
        if text:
            out.append(_dec(text))
    if not out:
        raise ValueError("no numbers given")
    return out[:cap]


# --------------------------------------------------------------- date utilities
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M", "%Y%m%d", "%d-%m-%Y", "%m/%d/%Y")
_OFFSET_UNITS = {"d": 1, "day": 1, "days": 1, "w": 7, "week": 7, "weeks": 7}


def _parse_date(text, allow_relative=True):
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty date")
    low = raw.lower()
    now = datetime.datetime.now()
    if allow_relative:
        if low in ("today", "now", "tod"):
            return now.date()
        if low == "tomorrow":
            return now.date() + datetime.timedelta(days=1)
        if low == "yesterday":
            return now.date() - datetime.timedelta(days=1)
    low = low.replace(" ", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    if allow_relative:
        # short offsets: +3, +3d, -2w (bare numbers are days)
        match = re.fullmatch(r"([+-])(\d+)([a-z]?)", low)
        if match:
            sign = -1 if match.group(1) == "-" else 1
            factor = _OFFSET_UNITS.get(match.group(3) or "d") if match.group(3) else 1
            if factor is None:
                raise ValueError("unknown offset unit %r (use d or w)" % match.group(3))
            return now.date() + datetime.timedelta(days=sign * int(match.group(2)) * factor)
        for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    raise ValueError("unrecognised date %r (use YYYY-MM-DD, YYYY/MM/DD, today, +3d)" % raw)


def _add_months(day, months):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    last = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][month - 1]
    return datetime.date(year, month, min(day.day, last))


_TZ_FALLBACK = {
    "utc": 0, "etc/utc": 0, "gmt": 0, "asia/shanghai": 8, "prc": 8, "asia/chongqing": 8,
    "asia/hong_kong": 8, "asia/taipei": 8, "asia/singapore": 8, "asia/tokyo": 9,
    "asia/seoul": 9, "australia/sydney": 10, "australia/perth": 8, "asia/kolkata": 5.5,
    "asia/calcutta": 5.5, "asia/dubai": 4, "asia/bangkok": 7, "asia/jakarta": 7,
    "europe/london": 0, "europe/paris": 1, "europe/berlin": 1, "europe/madrid": 1,
    "europe/moscow": 3, "africa/cairo": 2, "america/new_york": -5, "america/chicago": -6,
    "america/denver": -7, "america/los_angeles": -8, "america/sao_paulo": -3,
    "america/mexico_city": -6, "pacific/auckland": 12, "pacific/honolulu": -10,
}


def _zone(name):
    """(tzinfo, source label): zoneinfo when tzdata is present, fixed offset otherwise."""
    clean = str(name or "").strip()
    if not clean or clean.lower() == LOCAL_ZONE:
        return datetime.datetime.now().astimezone().tzinfo, "local"
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None
    if ZoneInfo is not None:
        try:
            return ZoneInfo(clean), "iana"
        except Exception:  # noqa: BLE001 - unknown zone, or tzdata unavailable
            pass
    hours = _TZ_FALLBACK.get(clean.lower())
    if hours is None:
        raise ValueError("unknown time zone %r (no tzdata and no fixed-offset entry)" % clean)
    return datetime.timezone(datetime.timedelta(hours=hours), clean), "fixed-offset"


def _stamp(when, tz):
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    offset = when.utcoffset()
    if offset is None:
        return stamp + " (naive)"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s UTC%s%02d:%02d" % (stamp, sign, total // 3600, (total % 3600) // 60)


def _business_days(start, end):
    step = 1 if end >= start else -1
    day, count = start, 0
    while day != end:
        day += datetime.timedelta(days=step)
        if day.weekday() < 5:
            count += 1
    return count


srv = Server("calc")


# ------------------------------------------------------------------------ tools
@srv.tool("calc", "Evaluate a safe arithmetic expression (+ - * / // % ** parens and math functions).",
          {"type": "object", "properties": {
              "expression": {"type": "string", "description": "e.g. 2+3*4, sqrt(16), log(1000), pi*2**3"},
              "precision": {"type": "integer", "default": 10, "description": "digits after the decimal point"}},
           "required": ["expression"]})
def calc(expression, precision=10):
    digits = _digits(precision)
    value = _evaluate(expression)
    rounded = _round(value, digits)
    return "raw=%s\nrounded=%s\n= %s\nprecision=%d" % (_clean(value), _clean(rounded), _clean(rounded), digits)


@srv.tool("unit_convert", "Convert length/mass/area/volume/speed/data/temperature/time units.",
          {"type": "object", "properties": {
              "value": {"type": "string", "description": "a number, or several separated by commas"},
              "from_unit": {"type": "string"},
              "to_unit": {"type": "string"}},
           "required": ["value", "from_unit", "to_unit"]})
def unit_convert(value, from_unit, to_unit):
    src, dst = _aliases(from_unit), _aliases(to_unit)
    numbers = _values(value)
    if src in TEMP_UNITS and dst in TEMP_UNITS:
        def temp(x):
            celsius = x if src in ("c",) else (x - 32) * D(5) / D(9) if src == "f" else x - D("273.15")
            if dst == "c":
                return celsius
            if dst == "f":
                return celsius * 9 / 5 + 32
            return celsius + D("273.15")
        category = "temperature"
        factor_note = "C/F/K"
        convert = temp
    else:
        name_a, table_a = _find_category(src)
        name_b, table_b = _find_category(dst)
        if table_a is None:
            raise ValueError("unknown unit %r (known: %s)" % (from_unit, ", ".join(sorted(set(LENGTH) | set(MASS)))))
        if table_b is None:
            raise ValueError("unknown unit %r (check the spelling or the category)" % to_unit)
        if name_a != name_b:
            raise ValueError("%r is %s but %r is %s - that conversion is not defined"
                             % (from_unit, name_a, to_unit, name_b))
        category = name_a
        # factors are "<unit> per base unit", so one source unit is worth
        # (source factor / destination factor) destination units
        ratio = _dec(table_a[src]) / _dec(table_b[dst])
        factor_note = "1 %s = %s %s" % (from_unit, _clean(_round(ratio, 12)), to_unit)
        convert = lambda x: x * ratio  # noqa: E731 - one-line conversion closure

    rows = []
    for number in numbers:
        result = _round(convert(number), 10)
        rows.append("%s %s = %s %s" % (_clean(_round(number, 10)), from_unit, _clean(result), to_unit))
    if len(numbers) > 1:
        head = ("category=%s inputs=%d %s -> %s factor=%s"
                % (category, len(numbers), from_unit, to_unit, factor_note))
    else:
        head = ("category=%s input=%s %s -> %s factor=%s"
                % (category, _clean(numbers[0]), from_unit, to_unit, factor_note))
    return head + "\n" + "\n".join(rows)


@srv.tool("base_convert", "Convert an integer between bases 2..36; 0x/0b/0o prefixes are understood.",
          {"type": "object", "properties": {
              "value": {"type": "string"},
              "from_base": {"type": "integer", "default": 10},
              "to_base": {"type": "integer", "default": 16}},
           "required": ["value"]})
def base_convert(value, from_base=10, to_base=16):
    raw = str(value or "").strip().replace("_", "")
    if not raw:
        raise ValueError("value is empty")
    source = int(from_base)
    body, negative = raw, False
    if body[:1] in "+-":
        negative, body = body[0] == "-", body[1:]
    low = body.lower()
    prefixed = {"0x": 16, "0b": 2, "0o": 8}
    prefix = next((p for p in prefixed if low.startswith(p)), "")
    if prefix:
        source = prefixed[prefix]
        body = body[2:]
    if not 2 <= source <= 36 or not 2 <= int(to_base) <= 36:
        raise ValueError("bases must be within 2..36")
    number = int(body, source)
    if negative:
        number = -number
    target = int(to_base)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if target == 10:
        rendered = str(number)
    else:
        n, out = abs(number), ""
        while n:
            out = digits[n % target] + out
            n //= target
        rendered = (out or "0") if number >= 0 else "-" + (out or "0")
        if target == 16:
            rendered = ("-0x" + rendered[1:]) if rendered.startswith("-") else "0x" + rendered
    return ("input=%r from_base=%d to_base=%d\nvalue=%d\ndecimal=%d\nbase%d=%s"
            % (raw, source, target, number, number, target, rendered))


@srv.tool("percent", "Percentage maths: of|change|diff|add|sub.",
          {"type": "object", "properties": {
              "operation": {"type": "string", "description": "of = a% of b; change = a->b % change; diff = relative difference; add = b + a%; sub = b - a%"},
              "a": {"type": "string"},
              "b": {"type": "string", "default": "0"}},
           "required": ["operation", "a"]})
def percent(operation, a, b=0):
    how = str(operation or "").strip().lower()
    if how not in ("of", "change", "diff", "add", "sub"):
        raise ValueError("operation must be of|change|diff|add|sub")
    x, y = _dec(a), _dec(b)
    if how == "of":
        result, note = x / 100 * y, "%s%% of %s" % (_clean(x), _clean(y))
    elif how == "change":
        if x == 0:
            raise ValueError("percentage change from 0 is undefined")
        result, note = (y - x) / x * 100, "change %s -> %s" % (_clean(x), _clean(y))
    elif how == "diff":
        base = (x + y) / 2
        if base == 0:
            result, note = D(0), "relative difference of two zeros"
        else:
            result, note = abs(y - x) / base * 100, "relative difference %s vs %s" % (_clean(x), _clean(y))
    elif how == "add":
        result, note = y * (1 + x / 100), "%s + %s%%" % (_clean(y), _clean(x))
    else:
        result, note = y * (1 - x / 100), "%s - %s%%" % (_clean(y), _clean(x))
    return ("operation=%s (%s)\na=%s b=%s\nresult=%s\n= %s%s"
            % (how, note, _clean(x), _clean(y), _clean(result),
               _clean(_round(result, 6)), "%" if how in ("change", "diff") else ""))


@srv.tool("date_diff", "Difference between two dates; business_days=true counts working days only.",
          {"type": "object", "properties": {
              "date_a": {"type": "string", "description": "YYYY-MM-DD, YYYY/MM/DD, today, tomorrow, +3d"},
              "date_b": {"type": "string", "default": "", "description": "empty = today"},
              "unit": {"type": "string", "default": "days", "description": "days|weeks|months|years|hours"},
              "business_days": {"type": "boolean", "default": False}},
           "required": ["date_a"]})
def date_diff(date_a, date_b="", unit="days", business_days=False):
    left = _parse_date(date_a)
    right = _parse_date(date_b) if str(date_b or "").strip() else datetime.date.today()
    days = (right - left).days
    how = str(unit or "days").strip().lower()
    table = {"days": D(days), "day": D(days), "weeks": D(days) / 7, "week": D(days) / 7,
             "months": D(days) / D("30.436875"), "years": D(days) / D("365.2425"),
             "hours": D(days) * 24}
    if how not in table:
        raise ValueError("unit must be days|weeks|months|years|hours")
    business = _business_days(left, right) if business_days else None
    out = ["date_a=%s\n date_b=%s\ndirection=%s" % (left, right, "b -> a" if days < 0 else "a -> b"),
           "days=%d\nunit=%s\nvalue=%s" % (days, how, _clean(_round(table[how], 4)))]
    if business is not None:
        out.append("business_days=%d\nweekend_days=%d" % (business, abs(days) - business))
    out.append("datediff_anchor=%s" % (right - left))
    return "\n".join(out)


@srv.tool("date_add", "Add or subtract an amount of days/weeks/months/years from a date.",
          {"type": "object", "properties": {
              "date": {"type": "string"},
              "amount": {"type": "integer", "default": 0},
              "unit": {"type": "string", "default": "days", "description": "days|weeks|months|years|hours"}},
           "required": ["date", "amount"]})
def date_add(date, amount, unit="days"):
    base = _parse_date(date)
    count = int(amount)
    how = str(unit or "days").strip().lower()
    if how in ("days", "day"):
        result = base + datetime.timedelta(days=count)
    elif how in ("weeks", "week"):
        result = base + datetime.timedelta(weeks=count)
    elif how in ("hours", "hour", "h"):
        result = base + datetime.timedelta(hours=count)
    elif how in ("months", "month"):
        result = _add_months(base, count)
    elif how in ("years", "year"):
        result = _add_months(base, count * 12)
    else:
        raise ValueError("unit must be days|weeks|months|years|hours")
    return ("input=%s\namount=%s %s\nresult=%s\nweekday=%s (%d)\ndays_moved=%d"
            % (base, count, how, result, DAY_NAMES[result.weekday()], result.weekday() + 1,
               (result - base).days))


@srv.tool("date_info", "Weekday, ISO week number, day of year and days left for a date.",
          {"type": "object", "properties": {"date": {"type": "string", "default": "today"}}})
def date_info(date="today"):
    day = _parse_date(date)
    iso = day.isocalendar()
    today = datetime.date.today()
    year_end = datetime.date(day.year, 12, 31)
    return ("date=%s\nweekday=%s (%d)\niso_week=%04d-W%02d-%d\nday_of_year=%d\ndays_in_year=%d\n"
            "days_left_in_year=%d\nquarter=Q%d\nleap_year=%s\ndays_since_today=%d\nage_in_days=%d"
            % (day, DAY_NAMES[day.weekday()], day.weekday() + 1, iso[0], iso[1], iso[2],
               day.timetuple().tm_yday, 366 if year_end.timetuple().tm_yday == 366 else 365,
               (year_end - day).days, (day.month - 1) // 3 + 1,
               day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0),
               (today - day).days, (today - day).days))


@srv.tool("timezone_now", "Current time in each requested zone (IANA names, fixed-offset fallback).",
          {"type": "object", "properties": {
              "zones": {"type": "string", "default": "Asia/Shanghai,UTC", "description": "comma separated zone names"}}})
def timezone_now(zones="Asia/Shanghai,UTC"):
    names = [z.strip() for z in str(zones or "").replace(";", ",").split(",") if z.strip()]
    names = names[:40] or ["UTC"]
    now = datetime.datetime.now(datetime.timezone.utc)
    rows, notes = [], []
    for name in names:
        tz, source = _zone(name)
        local = now.astimezone(tz)
        offset = local.utcoffset()
        notes.append("%-20s %s  %s  offset=%s  [%s]" % (name, local.strftime("%Y-%m-%d %H:%M:%S"),
                                                        local.tzname() or "-", offset, source))
        rows.append(local)
    tzinfo = set(str(r.tzinfo) for r in rows)
    head = ("utc_now=%s\nzones=%d distinct_tz=%d"
            % (now.strftime("%Y-%m-%d %H:%M:%S"), len(names), len(tzinfo)))
    return head + "\n" + "\n".join(notes)


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return D(0)
    position = (len(sorted_values) - 1) * _dec(fraction)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


@srv.tool("statistics", "count/sum/mean/median/min/max/stdev/percentiles for a list of numbers.",
          {"type": "object", "properties": {
              "values": {"type": "string", "description": "comma separated numbers, or JSON-ish list [1,2,3]"},
              "precision": {"type": "integer", "default": 4}},
           "required": ["values"]})
def statistics(values, precision=4):
    digits = _digits(precision)
    data = _values(values)
    total = sum(data, D(0))
    count = len(data)
    mean = total / count
    ordered = sorted(data)
    if count % 2:
        median = ordered[count // 2]
    else:
        median = (ordered[count // 2 - 1] + ordered[count // 2]) / 2
    variance = sum((x - mean) ** 2 for x in data) / count
    stdev = variance.sqrt() if variance > 0 else D(0)
    samples = ordered[:200]
    rows = [("count=%d\nsum=%s\nmean=%s\nmedian=%s\nmin=%s\nmax=%s\nrange=%s\n"
             "stdev_population=%s\nvariance=%s\ndistinct=%d")
            % (count, _clean(_round(total, digits)), _clean(_round(mean, digits)),
               _clean(_round(median, digits)), _clean(ordered[0]), _clean(ordered[-1]),
               _clean(_round(ordered[-1] - ordered[0], digits)), _clean(_round(stdev, digits)),
               _clean(_round(variance, digits)), len(set(data)))]
    rows.append("p25=%s\np50=%s\np75=%s\np90=%s\np99=%s"
                % tuple(_clean(_round(_percentile(samples, f), digits))
                        for f in ("0.25", "0.5", "0.75", "0.9", "0.99")))
    if isinstance(values, (list, tuple)) and len(values) > 200:
        rows.append("note=percentiles computed on the first 200 sorted values")
    return "\n".join(rows)


@srv.tool("round_to", "Round a number to N digits (half_up, half_even, half_down, up, down, ceil, floor).",
          {"type": "object", "properties": {
              "value": {"type": "string"},
              "digits": {"type": "integer", "default": 2},
              "mode": {"type": "string", "default": "half_up"}},
           "required": ["value"]})
def round_to(value, digits=2, mode="half_up"):
    how = str(mode or "half_up").strip().lower()
    if how not in _MODE:
        raise ValueError("mode must be one of %s" % ", ".join(sorted(_MODE)))
    number = _dec(value)
    rounded = _round(number, int(digits), how)
    return ("value=%s\ndigits=%s\nmode=%s\nrounded=%s\n= %s\ndelta=%s"
            % (_clean(number), int(digits), how, _clean(rounded), _clean(rounded),
               _clean(_round(rounded - number, 10))))


@srv.tool("random_int", "Random integers in a range, optionally unique.",
          {"type": "object", "properties": {
              "minimum": {"type": "integer"},
              "maximum": {"type": "integer"},
              "count": {"type": "integer", "default": 1},
              "unique": {"type": "boolean", "default": False, "description": "min..max must hold `count` values"}},
           "required": ["minimum", "maximum"]})
def random_int(minimum, maximum, count=1, unique=False):
    low, high, size = int(minimum), int(maximum), max(1, min(int(count), 1000))
    if low > high:
        low, high = high, low
    span = high - low + 1
    if unique:
        if size > span:
            raise ValueError("cannot draw %d unique integers from %d values (%d..%d)" % (size, span, low, high))
        values = random.sample(range(low, high + 1), size)
    else:
        values = [random.randint(low, high) for _ in range(size)]
    return ("range=%d..%d count=%d unique=%s\ntotal=%d\nvalues=%s"
            % (low, high, size, bool(unique), sum(values), ", ".join(str(v) for v in values)))


# ---------------------------------------------------------------------- samples
SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest", "calc")

SAMPLES = {
    "calc": {"expression": "2+3*4+sqrt(16)/2", "precision": 10},
    "unit_convert": {"value": "100", "from_unit": "km", "to_unit": "mi"},
    "base_convert": {"value": "0xff", "from_base": 16, "to_base": 10},
    "percent": {"operation": "change", "a": "100", "b": "150"},
    "date_diff": {"date_a": "2026-01-01", "date_b": "2026-12-31", "unit": "days", "business_days": True},
    "date_add": {"date": "today", "amount": 30, "unit": "days"},
    "date_info": {"date": "today"},
    "timezone_now": {"zones": "Asia/Shanghai,UTC,Asia/Tokyo"},
    "statistics": {"values": "1,2,3,4,5,6,7,8,9,10", "precision": 4},
    "round_to": {"value": "2.675", "digits": 2, "mode": "half_up"},
    "random_int": {"minimum": 1, "maximum": 49, "count": 6, "unique": True},
}

# Nothing here needs the network, credentials or a clock beyond the local one.
SAMPLES_OPTIONAL = set()


def self_test():
    """Run every sample locally and print one line per tool (for --self-test)."""
    failures = 0
    for name in SAMPLES:
        try:
            text = str(srv.tools[name]["fn"](**SAMPLES[name]))
            print("  ok   %-14s %s" % (name, (text.splitlines() or [""])[0][:160]))
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            print("  FAIL %-14s %s: %s" % (name, type(exc).__name__, exc))
    print("calc selftest: %d ok, %d failed, %d tools" % (len(SAMPLES) - failures, failures, len(srv.tools)))
    return 1 if failures else 0


def build():
    return srv


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        raise SystemExit(self_test())
    srv.run()
