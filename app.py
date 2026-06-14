import streamlit as st
import pandas as pd
import json
from rapidfuzz import process

# --------------------------
# Load Databases
# --------------------------
with open("reference_ranges.json", "r") as f:
    REF_DB = json.load(f)

with open("critical_values.json", "r") as f:
    CRITICAL_DB = json.load(f)

VALID_TESTS = list(REF_DB.keys())

# --------------------------
# Improved Functions
# --------------------------

def check_range(test, value, unit):
    """Check if value is within reference range. Requires matching unit."""
    if test not in REF_DB:
        return None
    expected_unit = REF_DB[test]["unit"]
    if unit != expected_unit:
        return None   # Unit mismatch → cannot interpret
    low = REF_DB[test]["low"]
    high = REF_DB[test]["high"]
    if value < low:
        return "Low"
    if value > high:
        return "High"
    return "Normal"

def check_unit(test, unit):
    """Return True if unit matches the reference database."""
    if test not in REF_DB:
        # Unknown test – cannot verify unit
        return True  # let other checks decide if they want to ignore
    return unit == REF_DB[test]["unit"]

def check_critical(test, value, unit):
    """Check if value is critical. Requires matching unit."""
    if test not in CRITICAL_DB:
        return False
    crit_entry = CRITICAL_DB[test]
    if unit != crit_entry.get("unit", ""):
        return False   # Unit mismatch, critical cannot be determined
    low = crit_entry["low"]
    high = crit_entry["high"]
    return value < low or value > high

def find_typo(test):
    """Find closest matching test name if the given one is not exact."""
    if test in VALID_TESTS:
        return None
    match = process.extractOne(test, VALID_TESTS)
    if match and match[1] > 80:   # similarity > 80%
        return match[0]
    return None

def audit_report(df):
    issues = []
    for _, row in df.iterrows():
        test_original = str(row["Test"]).strip()
        try:
            value = float(row["Result"])
        except ValueError:
            continue   # skip non-numeric results
        unit = str(row["Unit"]).strip()

        # Step 1: Typo correction
        corrected_test = find_typo(test_original)
        if corrected_test:
            issues.append({
                "Test": test_original,
                "Issue": f"Possible typo. Did you mean '{corrected_test}'?",
                "Severity": "Medium"
            })
            test_used = corrected_test
        else:
            test_used = test_original

        # Step 2: Unit mismatch (using corrected or original test name)
        if not check_unit(test_used, unit):
            issues.append({
                "Test": test_original,
                "Issue": f"Unit mismatch. Expected unit: {REF_DB.get(test_used, {}).get('unit', '?')}",
                "Severity": "High"
            })
            # Do not proceed with range / critical checks if unit is wrong
            continue

        # Step 3: Reference range check
        rng = check_range(test_used, value, unit)
        if rng == "Low":
            issues.append({
                "Test": test_original,
                "Issue": "Result below reference range",
                "Severity": "Medium"
            })
        elif rng == "High":
            issues.append({
                "Test": test_original,
                "Issue": "Result above reference range",
                "Severity": "Medium"
            })

        # Step 4: Critical value check
        if check_critical(test_used, value, unit):
            issues.append({
                "Test": test_original,
                "Issue": "⚠️ Critical value detected",
                "Severity": "High"
            })

    return pd.DataFrame(issues)

# --------------------------
# Streamlit UI
# --------------------------
st.set_page_config(page_title="Lab Report Auditor", layout="wide")
st.title("🔬 Laboratory Report Auditor – Rule‑Based Quality Check")

uploaded_file = st.file_uploader("📂 Upload Excel or CSV Report", type=["xlsx", "csv"])

if uploaded_file:
    if uploaded_file.name.endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)

    st.subheader("📋 Uploaded Data")
    st.dataframe(df)

    if st.button("🚀 Run Audit"):
        results = audit_report(df)

        st.subheader("📊 Audit Findings")
        if len(results) == 0:
            st.success("✅ No issues detected.")
        else:
            st.dataframe(results)

            # Simple quality score (adjust penalty weight as needed)
            penalty = len(results) * 5
            score = max(0, 100 - penalty)
            st.metric("📈 Quality Score", f"{score}%")
