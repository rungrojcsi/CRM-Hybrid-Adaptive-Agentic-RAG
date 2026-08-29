"""
Generate synthetic CDM fixture CSVs for offline testing.
Run once: python make_fixtures.py
Column names match model.json exactly (spaces preserved).
"""
import os
import csv
from pathlib import Path

BASE = Path(__file__).parent

FIXTURES = {
    "Dim_Account": {
        "cols": ["Account ID", "Parent Account ID", "Sales Person ID", "Company Name",
                 "Customer Code", "Website", "Annual Revenue", "No. of Employees",
                 "Industry Code", "Industry", "Customer Type Code", "Customer Type",
                 "Created Date", "Last Modified Date", "City", "Province", "Country",
                 "Territory Code", "Owner Name (Backup)", "Account Status"],
        "rows": [
            ["ACC-001", "", "SP-01", "Alpha Manufacturing Co.", "C001", "https://alpha.com",
             "5000000", "120", "1", "Manufacturing", "1", "Customer",
             "2024-01-15", "2025-03-01", "Bangkok", "Bangkok", "Thailand",
             "1", "Alex K.", "Active"],
            ["ACC-002", "", "SP-02", "Beta Electronics Ltd.", "C002", "https://beta.co.th",
             "2500000", "45", "2", "Electronics", "1", "Customer",
             "2024-06-01", "2025-04-10", "Chiang Mai", "Chiang Mai", "Thailand",
             "2", "Wanchai P.", "Active"],
        ],
    },
    "Dim_Contact": {
        "cols": ["Contact ID", "Account ID", "Sales Person ID", "Contact Name", "First Name",
                 "Last Name", "Job Title", "Department", "Gender Code", "Birth Date",
                 "Email", "Mobile Phone", "Office Phone", "Do Not Email", "Do Not Call",
                 "Created Date", "Last Modified Date", "Sales Person Name"],
        "rows": [
            ["CON-001", "ACC-001", "SP-01", "Nattaporn Siri", "Nattaporn", "Siri",
             "Procurement Manager", "Procurement", "2", "1985-03-20",
             "nattaporn@alpha.com", "081-111-1111", "02-111-1111", "False", "False",
             "2024-01-20", "2025-01-10", "Alex K."],
            ["CON-002", "ACC-001", "SP-01", "Krit Chanon", "Krit", "Chanon",
             "CEO", "Executive", "1", "1970-07-15",
             "krit@alpha.com", "081-222-2222", "02-111-2222", "False", "False",
             "2024-01-20", "2025-02-01", "Alex K."],
            ["CON-003", "ACC-002", "SP-02", "Pimsiri Nakorn", "Pimsiri", "Nakorn",
             "Purchasing Director", "Purchasing", "2", "1980-11-30",
             "pimsiri@beta.co.th", "089-333-3333", "", "False", "False",
             "2024-06-05", "2025-03-15", "Wanchai P."],
        ],
    },
    "Fact_Opportunity": {
        "cols": ["Opportunity ID", "Create Date", "Update Date", "Sales Person ID",
                 "Sale Person Name", "Opportunity Name", "Actual Close Date",
                 "Exchange Rate", "Possibility", "Description", "Est. Close Date",
                 "Solution Name", "Status", "Account ID", "Contact ID",
                 "Hot Opportunity", "End Customer ID", "SO Plan Date", "SO Actual Date",
                 "Project Code", "Total SO Plan Amount", "Total SO Actual Amount",
                 "Progress", "IsImportant", "Opportunity No", "Detail Reasons",
                 "Prospect Category ID", "Solution ID", "Prospect Category Name",
                 "SO Actual Amount", "SO Actual Amount (H)", "SO Actual Amount (K)",
                 "SO Actual Amount (P)", "SO Plan Amount", "SO Plan Amount (H)",
                 "SO Plan Amount (K)", "SO Plan Amount (P)", "Last Activity Date",
                 "Closed Date", "Sales Person Name", "Stage", "Win/Loss",
                 "Competitor", "Close Reason", "Stage Code"],
        "rows": [
            ["OPP-001", "2024-09-01", "2025-05-01", "SP-01", "Alex K.",
             "Alpha MES Phase 1", "", "1", "75", "MES implementation project",
             "2025-06-30", "MES Standard", "In Progress", "ACC-001", "CON-001",
             "True", "", "2025-04-01", "", "PRJ-2024-001",
             "3500000", "", "60", "True", "OPP-2024-001", "",
             "CAT-01", "SOL-01", "Manufacturing IT",
             "", "", "", "", "3500000", "500000", "2000000", "1000000",
             "2025-05-10", "", "Alex K.", "Proposal", "", "", "", "3"],
            ["OPP-002", "2024-11-15", "2025-04-20", "SP-02", "Wanchai P.",
             "Beta ERP Upgrade", "", "1", "50", "ERP system upgrade",
             "2025-08-31", "ERP Enterprise", "In Progress", "ACC-002", "CON-003",
             "False", "", "", "", "PRJ-2024-002",
             "1800000", "", "30", "False", "OPP-2024-002", "",
             "CAT-02", "SOL-02", "ERP",
             "", "", "", "", "1800000", "600000", "900000", "300000",
             "2025-04-15", "", "Wanchai P.", "Qualification", "", "", "", "2"],
        ],
    },
    "Fact_Activity": {
        "cols": ["Activity ID", "Sales Person ID", "Sales Person Name", "Regarding ID",
                 "Regarding Name", "Subject", "Activity Type Code", "Activity Type",
                 "State Code", "State Name", "Priority Code", "Duration (Mins)",
                 "Scheduled Date", "Actual End Date", "Created Date",
                 "Opportunity ID", "Activity Date"],
        "rows": [
            ["ACT-001", "SP-01", "Alex K.", "ACC-001", "Alpha Manufacturing Co.",
             "Initial Discovery Call", "1", "Phone Call", "1", "Completed", "1", "60",
             "2024-09-10", "2024-09-10", "2024-09-10", "", "2024-09-10"],
            ["ACT-002", "SP-01", "Alex K.", "OPP-001", "Alpha MES Phase 1",
             "Demo Presentation", "4", "Demo", "1", "Completed", "1", "120",
             "2024-10-05", "2024-10-05", "2024-10-05", "OPP-001", "2024-10-05"],
            ["ACT-003", "SP-01", "Alex K.", "ACC-001", "Alpha Manufacturing Co.",
             "Contract Follow-up", "2", "Email", "1", "Completed", "2", "15",
             "2025-03-01", "2025-03-01", "2025-03-01", "", "2025-03-01"],
        ],
    },
    "Dim_Connection": {
        "cols": ["Connection ID", "Opportunity ID", "Stakeholder ID", "From Entity Type",
                 "Stakeholder Entity Type", "Opportunity Role ID", "Stakeholder Role ID",
                 "Stakeholder Role Name", "Status Code", "Effective Date"],
        "rows": [
            ["CON-C001", "OPP-001", "CON-002", "opportunity", "contact",
             "1", "2", "Decision Maker", "1", "2024-09-15"],
        ],
    },
    "Dim_Review": {
        "cols": ["Opportunity Review ID", "Created On", "Created By", "Created by Name",
                 "Opportunity Number", "Opportunity ID", "Opportunity Name"],
        "rows": [
            ["REV-001", "2025-03-15", "SP-01", "Alex K.",
             "OPP-2024-001", "OPP-001", "Alpha MES Phase 1"],
        ],
    },
    "Dim_OpportunityClose": {
        "cols": ["Close Activity ID", "Opportunity ID", "Competitor ID",
                 "Closed By User ID", "Activity Subject", "Close Detail Reason",
                 "Actual Revenue (Close)", "Actual Close Date", "System Created Date"],
        "rows": [],
    },
}


def make():
    for entity, spec in FIXTURES.items():
        entity_dir = BASE / entity
        entity_dir.mkdir(exist_ok=True)
        path = entity_dir / "part-001.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(spec["cols"])
            writer.writerows(spec["rows"])
        print(f"  wrote {path} ({len(spec['rows'])} rows)")


if __name__ == "__main__":
    make()
    print("Fixtures written.")
