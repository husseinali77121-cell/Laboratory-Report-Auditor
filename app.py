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
# Functions
# --------------------------

def check_range(test, value):

    if test not in REF_DB:
        return None

    low = REF_DB[test]["low"]
    high = REF_DB[test]["high"]

    if value < low:
        return "Low"

    if value > high:
        return "High"

    return "Normal"


def check_unit(test, unit):

    if test not in REF_DB:
        return True

    return unit == REF_DB[test]["unit"]


def check_critical(test, value):

    if test not in CRITICAL_DB:
        return False

    low = CRITICAL_DB[test]["low"]
    high = CRITICAL_DB[test]["high"]

    return value < low or value > high


def find_typo(test):

    if test in VALID_TESTS:
        return None

    match = process.extractOne(
        test,
        VALID_TESTS
    )

    if match and match[1] > 80:
        return match[0]

    return None


def audit_report(df):

    issues = []

    for _, row in df.iterrows():

        test = str(row["Test"]).strip()

        try:
            value = float(row["Result"])
        except:
            continue

        unit = str(row["Unit"]).strip()

        typo = find_typo(test)

        if typo:

            issues.append({
                "Test": test,
                "Issue": f"Possible typo. Did you mean {typo}?",
                "Severity": "Medium"
            })

        if not check_unit(test, unit):

            issues.append({
                "Test": test,
                "Issue": "Unit mismatch",
                "Severity": "High"
            })

        rng = check_range(test, value)

        if rng == "Low":

            issues.append({
                "Test": test,
                "Issue": "Below reference range",
                "Severity": "Medium"
            })

        elif rng == "High":

            issues.append({
                "Test": test,
                "Issue": "Above reference range",
                "Severity": "Medium"
            })

        if check_critical(test, value):

            issues.append({
                "Test": test,
                "Issue": "Critical value detected",
                "Severity": "High"
            })

    return pd.DataFrame(issues)

# --------------------------
# Streamlit UI
# --------------------------

st.set_page_config(
    page_title="Lab Report Auditor",
    layout="wide"
)

st.title("🔬 Laboratory Report Auditor")

uploaded_file = st.file_uploader(
    "Upload Excel Report",
    type=["xlsx", "csv"]
)

if uploaded_file:

    if uploaded_file.name.endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)

    st.subheader("Uploaded Data")
    st.dataframe(df)

    if st.button("Run Audit"):

        results = audit_report(df)

        st.subheader("Audit Findings")

        if len(results) == 0:
            st.success("No issues detected.")
        else:
            st.dataframe(results)

            score = max(
                0,
                100 - len(results) * 5
            )

            st.metric(
                "Quality Score",
                f"{score}%"
            )
