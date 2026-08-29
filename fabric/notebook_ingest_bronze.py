# CSI CRM Quant — Bronze Power BI Dataflow CSV → Delta tables
# Paste into Fabric Notebook (PySpark Python kernel) attached to lh_crm_rs
# v2: handles invalid column chars + non-uniform folder structure + empty entities

# ============ CELL 1: discover entities ============
import re
from pyspark.sql import functions as F
from pyspark.sql.window import Window
from notebookutils import mssparkutils

SOURCE_BASE = "abfss://bronze@crmpocrs.dfs.core.windows.net/CSI DATA PLATFORM/SALE DATA CLEANSING"

entities = sorted([
    item.name.rstrip('/')
    for item in mssparkutils.fs.ls(SOURCE_BASE)
    if item.isDir and item.name.startswith("Dim_")
])
print(f"Found {len(entities)} entities:")
for e in entities:
    print(f"  - {e}")


# ============ CELL 2: ingest each entity to Delta (v4 — fix dedup) ============
import re

def sanitize(c):
    return re.sub(r'[ ,;{}()\n\t=]', '_', c)

def ing(e):
    base = f"{SOURCE_BASE}/{e}"
    try:
        df = (spark.read
              .option("header", True)
              .option("inferSchema", True)
              .option("recursiveFileLookup", True)
              .csv(base))
    except Exception as ex:
        return f"READ_FAIL: {str(ex)[:80]}"
    if len(df.columns) == 0:
        return "EMPTY"
    df = df.toDF(*[sanitize(c) for c in df.columns])
    df = df.withColumn("_fp", F.input_file_name())
    df = df.withColumn("_snap", F.regexp_extract("_fp", r"@snapshot=([^/]+)", 1))
    df = df.withColumn("_part", F.regexp_extract("_fp", r"/part-([^@/]+)\.csv", 1))
    # v4 fix: keep ALL rows from latest snapshot per partition (not just 1 row)
    latest = df.groupBy("_part").agg(F.max("_snap").alias("_max_snap"))
    d = (df.join(latest, (df._part == latest._part) & (df._snap == latest._max_snap), "inner")
            .drop(latest._part, "_max_snap", "_fp", "_part", "_snap"))
    n = d.count()
    if n == 0:
        return "ZERO_ROWS"
    d.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(e.lower())
    return n

for e in entities:
    try:
        r = ing(e)
        print(f"{'OK' if isinstance(r, int) else 'SKIP'} {e.lower()}: {r}")
    except Exception as ex:
        print(f"FAIL {e}: {str(ex)[:120]}")
print("Done")
