"""
Orange Lab - Report Reviewer
==============================
يراجع تقارير المعمل (CBC, RFT, LFT, Lipid Profile, Thyroid Profile...) ويكتشف:
  1) أخطاء في Reference Range (مختلف عن المرجع المعتمد للسن/الجنس)
  2) أخطاء في اسم التحليل (إملائي / غير معتمد)
  3) ترتيب عرض غير صحيح داخل البروفايل
  4) عدم تطابق Flag (H/L/N) مع النتيجة الفعلية
  5) قيم حرجة (Critical Values) بدون توثيق
  6) وحدة قياس غير متطابقة مع الوحدة المعتمدة

المرجع المستخدم لكل Check موضح في عمود "المصدر" بالنتائج.
"""

import json
import re
import difflib
from pathlib import Path

import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------------
# تحميل قاعدة البيانات المرجعية
# --------------------------------------------------------------------------------

DATA_PATH = Path(__file__).parent / "data" / "reference_ranges.json"


@st.cache_data
def load_reference_db():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


REF_DB = load_reference_db()
TESTS = REF_DB["tests"]
PANELS = REF_DB["panels"]

# بناء خريطة بحث: كل اسم/alias (lowercase) -> الاسم القياسي (canonical)
NAME_LOOKUP = {}
for t in TESTS:
    NAME_LOOKUP[t["canonical_name"].strip().lower()] = t["canonical_name"]
    for alias in t.get("aliases", []):
        NAME_LOOKUP[alias.strip().lower()] = t["canonical_name"]

TEST_BY_NAME = {t["canonical_name"]: t for t in TESTS}


# --------------------------------------------------------------------------------
# دوال مساعدة
# --------------------------------------------------------------------------------

RANGE_PATTERN = re.compile(r"(-?\d+\.?\d*)\s*[-–—]\s*(-?\d+\.?\d*)")
LESS_THAN_PATTERN = re.compile(r"[<≤]\s*(-?\d+\.?\d*)")
GREATER_THAN_PATTERN = re.compile(r"[>≥]\s*(-?\d+\.?\d*)")
NUMERIC_PATTERN = re.compile(r"^-?\d+\.?\d*$")


def normalize_unit(unit_text: str) -> str:
    if not unit_text:
        return ""
    u = unit_text.strip().lower()
    u = u.replace("μ", "u").replace("µ", "u")
    u = u.replace(" ", "")
    u = u.replace("x10^3", "10^3").replace("x10^6", "10^6")
    u = u.replace("10*3", "10^3").replace("10*6", "10^6")
    return u


def parse_reference_range_text(text: str):
    """يحاول استخراج (low, high) من نص الرينج المكتوب في التقرير."""
    if not text:
        return None, None
    text = text.strip()
    m = RANGE_PATTERN.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = LESS_THAN_PATTERN.search(text)
    if m:
        return None, float(m.group(1))
    m = GREATER_THAN_PATTERN.search(text)
    if m:
        return float(m.group(1)), None
    return None, None


def find_canonical_name(raw_name: str, cutoff: float = 0.78):
    """يطابق اسم التحليل المكتوب مع الاسم القياسي. يرجع (canonical, exact_match, suggestion)."""
    if not raw_name:
        return None, False, None
    key = raw_name.strip().lower()
    if key in NAME_LOOKUP:
        return NAME_LOOKUP[key], True, None

    # محاولة مطابقة تقريبية (احتمال خطأ إملائي)
    matches = difflib.get_close_matches(key, NAME_LOOKUP.keys(), n=1, cutoff=cutoff)
    if matches:
        suggestion = NAME_LOOKUP[matches[0]]
        return None, False, suggestion

    return None, False, None


def get_expected_range(test_def: dict, age, sex):
    """يرجع (low, high, matched_rule) أنسب رينج للسن/الجنس."""
    best = None
    best_score = -1
    for rule in test_def.get("ranges", []):
        rule_sex = rule.get("sex", "any")
        age_min = rule.get("age_min", 0)
        age_max = rule.get("age_max", 120)

        if age is not None and not (age_min <= age <= age_max):
            continue

        if rule_sex == "any":
            sex_score = 1
        elif sex and rule_sex == sex:
            sex_score = 2
        else:
            continue

        if sex_score > best_score:
            best_score = sex_score
            best = rule

    if best is None:
        # fallback: أي رينج "any" بدون قيد سن
        for rule in test_def.get("ranges", []):
            if rule.get("sex", "any") == "any":
                best = rule
                break

    if best is None:
        return None, None, None
    return best.get("low"), best.get("high"), best


def determine_flag(result, low, high):
    if result is None:
        return None
    if low is not None and result < low:
        return "L"
    if high is not None and result > high:
        return "H"
    return "N"


def parse_pasted_report(raw_text: str):
    """
    يحلل التقرير الملصق سطر بسطر.
    الصيغة المتوقعة لكل سطر (مفصولة بمسافتين أو أكثر أو Tab):
        اسم التحليل   النتيجة   الوحدة   الرينج المكتوب   [Flag]
    """
    rows = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # تقسيم بالمسافات المتعددة أو التابات
        parts = re.split(r"\t+|\s{2,}", line)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 2:
            continue

        name = parts[0]
        result_raw = parts[1] if len(parts) > 1 else ""
        unit = parts[2] if len(parts) > 2 else ""
        ref_range = parts[3] if len(parts) > 3 else ""
        flag = parts[4] if len(parts) > 4 else ""

        rows.append({
            "Test (as written)": name,
            "Result (raw)": result_raw,
            "Unit (as written)": unit,
            "Reference Range (as written)": ref_range,
            "Flag (as written)": flag,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------
# محرك الفحص (Checks Engine)
# --------------------------------------------------------------------------------

def run_checks(df: pd.DataFrame, age, sex):
    issues = []
    recognized_sequence = []  # [(canonical_name, row_index)]

    for idx, row in df.iterrows():
        raw_name = str(row.get("Test (as written)", "")).strip()
        result_raw = str(row.get("Result (raw)", "")).strip()
        unit_raw = str(row.get("Unit (as written)", "")).strip()
        range_raw = str(row.get("Reference Range (as written)", "")).strip()
        flag_raw = str(row.get("Flag (as written)", "")).strip().upper()

        if not raw_name:
            continue

        canonical, exact, suggestion = find_canonical_name(raw_name)

        # ---------- (2) فحص اسم التحليل ----------
        if canonical is None and suggestion:
            issues.append({
                "التحليل (كما كُتب)": raw_name,
                "نوع الخطأ": "اسم تحليل خاطئ",
                "التفاصيل": f"الاسم غير معتمد، الاسم القياسي المقترح: '{suggestion}'",
                "كيفية العلاج": f"تصحيح الاسم إلى '{suggestion}' في القالب/النظام لضمان التوحيد مع الترميز العالمي LOINC.",
                "المصدر": "IFCC / LOINC naming recommendations",
            })
            canonical = suggestion  # نواصل الفحص بالاسم المقترح
        elif canonical is None and suggestion is None:
            issues.append({
                "التحليل (كما كُتب)": raw_name,
                "نوع الخطأ": "اسم تحليل غير معروف",
                "التفاصيل": "لم يتم التعرف على هذا الاسم في قاعدة البيانات المرجعية.",
                "كيفية العلاج": "إضافة التحليل لقاعدة البيانات إن كان صحيحًا، أو مراجعة الاسم وتصحيحه.",
                "المصدر": "Internal database / LOINC",
            })
            continue  # لا يمكن إجراء باقي الفحوصات بدون تعريف

        test_def = TEST_BY_NAME.get(canonical)
        if not test_def:
            continue

        recognized_sequence.append((canonical, idx))

        exp_low, exp_high, _ = get_expected_range(test_def, age, sex)
        report_low, report_high = parse_reference_range_text(range_raw)

        # ---------- (1) فحص Reference Range ----------
        if range_raw:
            mismatch = False
            if exp_low is not None and report_low is not None and abs(exp_low - report_low) > 1e-6:
                mismatch = True
            if exp_high is not None and report_high is not None and abs(exp_high - report_high) > 1e-6:
                mismatch = True
            if mismatch:
                exp_text = f"{exp_low if exp_low is not None else ''}-{exp_high if exp_high is not None else ''}"
                issues.append({
                    "التحليل (كما كُتب)": raw_name,
                    "نوع الخطأ": "Reference Range خاطئ",
                    "التفاصيل": (
                        f"الرينج المكتوب '{range_raw}' لا يطابق الرينج المعتمد "
                        f"للسن/الجنس المحددين ({exp_text} {test_def.get('unit','')})."
                    ),
                    "كيفية العلاج": (
                        "تحديث الرينج في القالب ليطابق الفئة العمرية والنوع الصحيحين، "
                        "أو توثيق رينج المعمل الخاص (in-house) إن تم تطبيقه فعليًا بعد دراسة تحقق."
                    ),
                    "المصدر": "CLSI EP28-A3c (Defining Reference Intervals)",
                })
        elif exp_low is not None or exp_high is not None:
            issues.append({
                "التحليل (كما كُتب)": raw_name,
                "نوع الخطأ": "Reference Range مفقود",
                "التفاصيل": "لم يُكتب رينج مرجعي بجانب النتيجة.",
                "كيفية العلاج": (
                    f"إضافة الرينج المعتمد: "
                    f"{exp_low if exp_low is not None else ''}-{exp_high if exp_high is not None else ''} "
                    f"{test_def.get('unit','')}"
                ),
                "المصدر": "CLSI EP28-A3c (Defining Reference Intervals)",
            })

        # ---------- تحويل النتيجة لرقم لإجراء الفحوصات الرقمية ----------
        result_val = None
        if NUMERIC_PATTERN.match(result_raw):
            result_val = float(result_raw)

        # ---------- (4) فحص الوحدة ----------
        if unit_raw:
            norm_reported = normalize_unit(unit_raw)
            norm_expected = normalize_unit(test_def.get("unit", ""))
            alt_units = test_def.get("alt_units", [])
            alt_normalized = {normalize_unit(a["unit"]): a["factor"] for a in alt_units}

            if norm_reported != norm_expected and norm_reported not in alt_normalized:
                issues.append({
                    "التحليل (كما كُتب)": raw_name,
                    "نوع الخطأ": "وحدة قياس غير متطابقة",
                    "التفاصيل": f"الوحدة المكتوبة '{unit_raw}' لا تطابق الوحدة المعتمدة '{test_def.get('unit','')}'.",
                    "كيفية العلاج": f"تصحيح الوحدة إلى '{test_def.get('unit','')}' أو التأكد من تحويل القيمة والرينج معًا بنفس الوحدة.",
                    "المصدر": "SI Units / IFCC Unit Recommendations",
                })
            elif norm_reported in alt_normalized and result_val is not None:
                # النتيجة بوحدة بديلة - تحويلها للوحدة الأساسية ومراجعتها كذلك
                factor = alt_normalized[norm_reported]
                converted = result_val / factor if factor else None
                if converted is not None:
                    issues.append({
                        "التحليل (كما كُتب)": raw_name,
                        "نوع الخطأ": "وحدة قياس بديلة (SI)",
                        "التفاصيل": (
                            f"النتيجة مكتوبة بوحدة '{unit_raw}' (= {converted:.2f} {test_def.get('unit','')}). "
                            f"تأكد أن الرينج المعروض بنفس وحدة النتيجة."
                        ),
                        "كيفية العلاج": "توحيد وحدة النتيجة والرينج المعروضين معًا لتجنب سوء التفسير.",
                        "المصدر": "IFCC Unit Recommendations",
                    })
                    # نستخدم القيمة المحوّلة لفحوصات الفلاج والقيم الحرجة
                    result_val = converted

        # ---------- (5) القيم الحرجة ----------
        crit_low = test_def.get("critical_low")
        crit_high = test_def.get("critical_high")
        if result_val is not None:
            if (crit_low is not None and result_val <= crit_low) or (crit_high is not None and result_val >= crit_high):
                issues.append({
                    "التحليل (كما كُتب)": raw_name,
                    "نوع الخطأ": "قيمة حرجة (Critical Value)",
                    "التفاصيل": f"النتيجة {result_val} {test_def.get('unit','')} تقع في النطاق الحرج (≤{crit_low} أو ≥{crit_high}).",
                    "كيفية العلاج": "يجب التأكد من توثيق إشعار هاتفي فوري للطبيب المعالج (Critical Value Notification) في السجل.",
                    "المصدر": "CLSI GP47 (Critical Value Notification)",
                })

        # ---------- فحص تطابق Flag مع القيمة ----------
        if result_val is not None and flag_raw:
            calc_flag = determine_flag(result_val, exp_low, exp_high)
            flag_map = {"H": "H", "HIGH": "H", "مرتفع": "H",
                        "L": "L", "LOW": "L", "منخفض": "L",
                        "N": "N", "NORMAL": "N", "طبيعي": "N", "": "N"}
            reported_flag_norm = flag_map.get(flag_raw, flag_raw)
            if calc_flag and reported_flag_norm and calc_flag != reported_flag_norm:
                issues.append({
                    "التحليل (كما كُتب)": raw_name,
                    "نوع الخطأ": "Flag غير متطابق مع النتيجة",
                    "التفاصيل": f"النتيجة {result_val} مع الرينج المعتمد تشير إلى '{calc_flag}'، لكن المكتوب '{flag_raw}'.",
                    "كيفية العلاج": "إعادة حساب الـ Flag تلقائيًا من النظام بدلًا من الإدخال اليدوي لتجنب التضارب.",
                    "المصدر": "Internal QA / Result-Flag Consistency Check",
                })

    # ---------- (3) فحص ترتيب العرض داخل البروفايل ----------
    recognized_names = [c for c, _ in recognized_sequence]
    for panel_name, panel_order in PANELS.items():
        present_in_panel = [n for n in recognized_names if n in panel_order]
        if len(present_in_panel) < 2:
            continue
        # الترتيب المتوقع لنفس العناصر الموجودة فقط
        expected_subseq = [n for n in panel_order if n in present_in_panel]
        # الترتيب الفعلي كما ظهر في التقرير (مع إزالة التكرار مع الحفاظ على الترتيب)
        seen = set()
        actual_subseq = []
        for n in present_in_panel:
            if n not in seen:
                actual_subseq.append(n)
                seen.add(n)

        if actual_subseq != expected_subseq:
            issues.append({
                "التحليل (كما كُتب)": f"بروفايل {panel_name}",
                "نوع الخطأ": "ترتيب عرض غير صحيح",
                "التفاصيل": (
                    f"الترتيب الحالي: {' → '.join(actual_subseq)}\n"
                    f"الترتيب الصحيح المعتاد: {' → '.join(expected_subseq)}"
                ),
                "كيفية العلاج": f"إعادة ترتيب عناصر بروفايل '{panel_name}' حسب الترتيب المعياري المعتمد في التقرير.",
                "المصدر": "CAP Checklist - Report Layout Standardization",
            })

    return pd.DataFrame(issues)


# --------------------------------------------------------------------------------
# واجهة Streamlit
# --------------------------------------------------------------------------------

st.set_page_config(page_title="Orange Lab - Report Reviewer", page_icon="🟠", layout="wide")

st.title("🟠 Orange Lab - مراجع التقارير المعملية")
st.caption(
    "أداة مساعدة لمراجعة تقارير المعمل واكتشاف الأخطاء العلمية الشائعة "
    "(Reference Range، أسماء التحاليل، الترتيب، الوحدات، القيم الحرجة)."
)

with st.sidebar:
    st.header("بيانات المريض")
    age = st.number_input("السن (سنوات)", min_value=0, max_value=120, value=30, step=1)
    sex = st.radio("النوع", options=["M", "F"], format_func=lambda x: "ذكر" if x == "M" else "أنثى", horizontal=True)

    st.divider()
    st.header("قاعدة البيانات المرجعية")
    st.write(f"عدد التحاليل المسجلة: **{len(TESTS)}**")
    with st.expander("عرض كل التحاليل المعتمدة"):
        st.dataframe(
            pd.DataFrame([{"التحليل": t["canonical_name"], "الفئة": t.get("category", ""), "الوحدة": t.get("unit", "")} for t in TESTS]),
            use_container_width=True, hide_index=True,
        )

st.subheader("1) إدخال التقرير")

input_method = st.radio(
    "طريقة الإدخال",
    options=["لصق نصي (سطر لكل تحليل)", "رفع ملف Excel / CSV"],
    horizontal=True,
)

df = None

if input_method == "لصق نصي (سطر لكل تحليل)":
    st.caption(
        "كل سطر يمثل تحليل واحد بالترتيب: **اسم التحليل   النتيجة   الوحدة   الرينج   Flag (اختياري)** "
        "مفصولين بمسافتين أو أكثر / Tab."
    )
    example = (
        "Hemoglobin\t11.0\tg/dL\t13-17\tL\n"
        "WBC\t7.5\t10^3/uL\t4-11\n"
        "Hemglobin\t11.0\tg/dL\t13-17\n"
        "Creatinine\t1.5\tmg/dL\t0.7-1.3\tH\n"
        "ALT\t30\tU/L\t7-55"
    )
    raw_text = st.text_area("ألصق التقرير هنا:", value=example, height=200)
    if raw_text.strip():
        df = parse_pasted_report(raw_text)

else:
    uploaded = st.file_uploader("رفع ملف يحتوي على أعمدة: Test, Result, Unit, Reference Range, Flag", type=["csv", "xlsx"])
    if uploaded is not None:
        if uploaded.name.endswith(".csv"):
            raw_df = pd.read_csv(uploaded)
        else:
            raw_df = pd.read_excel(uploaded)

        # إعادة تسمية مرنة للأعمدة
        rename_map = {}
        for col in raw_df.columns:
            c = str(col).strip().lower()
            if c in ["test", "test name", "analyte", "اسم التحليل"]:
                rename_map[col] = "Test (as written)"
            elif c in ["result", "value", "النتيجة"]:
                rename_map[col] = "Result (raw)"
            elif c in ["unit", "units", "الوحدة"]:
                rename_map[col] = "Unit (as written)"
            elif c in ["reference range", "ref range", "normal range", "range", "الرينج"]:
                rename_map[col] = "Reference Range (as written)"
            elif c in ["flag", "h/l", "العلم"]:
                rename_map[col] = "Flag (as written)"

        raw_df = raw_df.rename(columns=rename_map)
        for needed in ["Test (as written)", "Result (raw)", "Unit (as written)", "Reference Range (as written)", "Flag (as written)"]:
            if needed not in raw_df.columns:
                raw_df[needed] = ""
        df = raw_df[["Test (as written)", "Result (raw)", "Unit (as written)", "Reference Range (as written)", "Flag (as written)"]].fillna("")

if df is not None and not df.empty:
    st.markdown("**البيانات بعد التحليل:**")
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("2) نتائج المراجعة")

    issues_df = run_checks(df, age=age, sex=sex)

    if issues_df.empty:
        st.success("✅ لم يتم اكتشاف أي أخطاء وفق الفحوصات الحالية.")
    else:
        st.warning(f"⚠️ تم اكتشاف {len(issues_df)} ملاحظة/خطأ.")
        st.dataframe(issues_df, use_container_width=True, hide_index=True)

        # تجميع حسب نوع الخطأ
        with st.expander("📊 ملخص حسب نوع الخطأ"):
            summary = issues_df["نوع الخطأ"].value_counts().reset_index()
            summary.columns = ["نوع الخطأ", "العدد"]
            st.dataframe(summary, use_container_width=True, hide_index=True)

        csv = issues_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ تحميل تقرير الأخطاء (CSV)", data=csv, file_name="report_review_issues.csv", mime="text/csv")
else:
    st.info("أدخل/ألصق بيانات التقرير لبدء المراجعة.")

st.divider()
st.caption(
    "ملحوظة: القيم المرجعية والقيم الحرجة المستخدمة هنا تمثل قيمًا عامة شائعة الاستخدام "
    "(CLSI / NCEP ATP III / ADA) كنقطة بداية، ويجب مراجعتها واعتمادها وفق دراسات التحقق "
    "الخاصة بكل معمل (Verification/Validation of Reference Intervals - CLSI EP28-A3c)."
)
