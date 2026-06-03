import boto3
import json
import csv
from io import StringIO
from datetime import datetime, timezone, timedelta

# ==========================================
# AWS CLIENT
# ==========================================

s3 = boto3.client('s3')

# ==========================================
# CONFIG
# ==========================================

BUCKET_NAME = "niku-auto-dashboard-pipeline"
INCOMING_PREFIX = "incoming/"
MASTER_FILE_KEY = "master/master_sales_data.csv"
ALERT_FILE_KEY = "alerts/alerts.csv"

IST = timezone(timedelta(hours=5, minutes=30))
DROP_THRESHOLD = -10  # Alert if drop is 10% or more

# ==========================================
# HELPER: SAFE DATETIME PARSER
# ==========================================

def parse_date(date_str):
    """
    Safely parses incoming date strings into datetime objects for logical evaluation.
    """
    if not date_str:
        return None
    
    formats = [
        "%A, %B %d, %Y",   # Example: Thursday, April 30, 2026
        "%Y-%m-%d",        # Example: 2026-04-30
        "%m/%d/%Y",        # Example: 04/30/2026
        "%Y-%m-%d %H:%M:%S"
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None

# ==========================================
# MAIN LAMBDA HANDLER
# ==========================================

def lambda_handler(event, context):

    print("===== EVENT START =====")
    print(json.dumps(event, indent=2))
    print("===== EVENT END =====")

    print("Lambda Started")

    all_rows = []
    seen_records = set()

    response = s3.list_objects_v2(
        Bucket=BUCKET_NAME,
        Prefix=INCOMING_PREFIX
    )

    if "Contents" not in response:
        create_empty_master()
        create_empty_alerts()

        return {
            "statusCode": 200,
            "body": "No files found"
        }

    # ==========================================
    # LOOP THROUGH FILES
    # ==========================================

    for obj in response["Contents"]:
        key = obj["Key"]

        if key.endswith("/"):
            continue

        if not (key.lower().endswith(".csv") or key.lower().endswith(".txt")):
            continue

        filename = key.split("/")[-1]
        print(f"Processing {filename}")

        file_upload_date = obj["LastModified"].astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")

        try:
            file_obj = s3.get_object(Bucket=BUCKET_NAME, Key=key)
            content = file_obj["Body"].read().decode("utf-8-sig", errors="ignore")

            lines = [line for line in content.splitlines() if line.strip()]

            if len(lines) <= 1:
                print(f"Skipped Empty File: {filename}")
                continue

            reader = csv.DictReader(lines)

            for row in reader:
                # Data Extraction
                order_id = str(row.get("order_id", "")).strip()
                sales = str(row.get("sales", "")).strip()
                region = str(row.get("region", "")).strip()
                product = str(row.get("product", "")).strip()
                category = str(row.get("category", "")).strip()
                order_date_raw = str(row.get("order_date", "")).strip()

                if not order_id or not sales or not region or not product or not category:
                    continue

                # Unique compound key matching cross-file structural updates
                record_id = f"{order_id}_{region}_{product}_{category}_{order_date_raw}"

                if record_id in seen_records:
                    continue

                seen_records.add(record_id)
                load_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

                all_rows.append({
                    "record_id": record_id,
                    "source_file": filename,
                    "file_upload_date": file_upload_date,
                    "load_timestamp": load_time,
                    "order_id": order_id,
                    "order_date": order_date_raw,
                    "region": region,
                    "product": product,
                    "category": category,
                    "quantity": str(row.get("quantity", "")).strip(),
                    "price": str(row.get("price", "")).strip(),
                    "discount": str(row.get("discount", "")).strip(),
                    "sales": sales,
                    "profit": str(row.get("profit", "")).strip(),
                    "shipping_cost": str(row.get("shipping_cost", "")).strip()
                })

        except Exception as e:
            print(f"Error Processing {filename}: {str(e)}")

    # Fetch previous data from Master file
    previous_rows = fetch_previous_master_data()

    # ENHANCEMENT: Added alert_id field to table schema headers
    alert_fieldnames = [
        "alert_id", "alert_date", "current_date", "previous_date", "region", "product",
        "category", "current_revenue", "previous_revenue", "impact_amount",
        "change_percent", "severity", "status"
    ]

    alerts = []

    if previous_rows is not None and len(previous_rows) > 0 and len(all_rows) > 0:
        
        # Parse datetimes internally for accurate timeline calculations
        for p_row in previous_rows:
            p_row["_order_datetime"] = parse_date(p_row.get("order_date", ""))

        valid_dates = []
        for row in all_rows:
            dt = parse_date(row.get("order_date", ""))
            if dt:
                row["_order_datetime"] = dt
                valid_dates.append(dt)
        
        if valid_dates:
            latest_datetime = max(valid_dates)
            latest_rows = [row for row in all_rows if row.get("_order_datetime") == latest_datetime]
            latest_date_str = latest_rows[0]["order_date"]

            # Isolate the absolute nearest preceding historical tracking date
            prior_dates = sorted({
                row["_order_datetime"] for row in previous_rows
                if row.get("_order_datetime") and row["_order_datetime"] < latest_datetime
            })

            if prior_dates:
                previous_datetime = prior_dates[-1]
                
                # ENHANCEMENT: Target date telemetry logs for clean CloudWatch debugging
                print(f"Current Date: {latest_datetime}")
                print(f"Previous Date: {previous_datetime}")
                
                prior_day_rows = [row for row in previous_rows if row.get("_order_datetime") == previous_datetime]
                previous_date_str = prior_day_rows[0]["order_date"]

                print(f"Comparing Current: {latest_date_str} vs Immediately Preceding: {previous_date_str}")

                # Group and aggregate total metrics for Current Processing Day
                current_sales_agg = {}
                for row in latest_rows:
                    agg_key = (row["region"], row["product"], row["category"])
                    current_sales_agg[agg_key] = current_sales_agg.get(agg_key, 0.0) + float(row.get("sales", 0) or 0)

                # Group and aggregate total metrics for the Isolated Previous Day
                previous_sales_agg = {}
                for row in prior_day_rows:
                    agg_key = (row["region"], row["product"], row["category"])
                    previous_sales_agg[agg_key] = previous_sales_agg.get(agg_key, 0.0) + float(row.get("sales", 0) or 0)

                # Generate Alerts based on Aggregated daily performance blocks
                for agg_key, current_revenue in current_sales_agg.items():
                    if agg_key in previous_sales_agg:
                        previous_revenue = previous_sales_agg[agg_key]
                        
                        if previous_revenue > 0:
                            drop_pct = ((current_revenue - previous_revenue) / previous_revenue) * 100

                            if drop_pct <= DROP_THRESHOLD:
                                impact_amount = previous_revenue - current_revenue
                                region, product, category = agg_key

                                # Tiered Severity Map
                                if drop_pct <= -50:
                                    severity = "CRITICAL"
                                elif drop_pct <= -25:
                                    severity = "HIGH"
                                elif drop_pct <= -10:
                                    severity = "MODERATE"
                                else:
                                    severity = "LOW"

                                # ENHANCEMENT: Clean space-stripped unique alert ID string creation
                                clean_reg = region.replace(" ", "_")
                                clean_prod = product.replace(" ", "_")
                                clean_date = latest_date_str.replace(" ", "_").replace(",", "")
                                alert_id = f"ALT_{clean_reg}_{clean_prod}_{clean_date}"

                                alerts.append({
                                    "alert_id": alert_id,
                                    "alert_date": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                                    "current_date": latest_date_str,
                                    "previous_date": previous_date_str,
                                    "region": region,
                                    "product": product,
                                    "category": category,
                                    "current_revenue": round(current_revenue, 2),
                                    "previous_revenue": round(previous_revenue, 2),
                                    "impact_amount": round(impact_amount, 2),
                                    "change_percent": round(drop_pct, 2),
                                    "severity": severity,
                                    "status": "OPEN"
                                })
                                print(f"Alert Generated [{alert_id}]: {region} | {product} dropped {drop_pct:.2f}%")

    # ==========================================
    # SAVE MASTER FILE
    # ==========================================
    master_fieldnames = [
        "record_id", "source_file", "file_upload_date", "load_timestamp",
        "order_id", "order_date", "region", "product", "category",
        "quantity", "price", "discount", "sales", "profit", "shipping_cost"
    ]

    master_output = StringIO()
    master_writer = csv.DictWriter(master_output, fieldnames=master_fieldnames, extrasaction='ignore')
    master_writer.writeheader()
    master_writer.writerows(all_rows)

    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=MASTER_FILE_KEY,
        Body=master_output.getvalue(),
        ContentType="text/csv"
    )

    # ==========================================
    # SAVE ALERT FILE
    # ==========================================
    alert_output = StringIO()
    alert_writer = csv.DictWriter(alert_output, fieldnames=alert_fieldnames, extrasaction='ignore')
    alert_writer.writeheader()
    alert_writer.writerows(alerts)

    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=ALERT_FILE_KEY,
        Body=alert_output.getvalue(),
        ContentType="text/csv"
    )
    print(f"Saved {len(alerts)} aggregated alerts")

    return {
        "statusCode": 200,
        "body": f"Success - {len(all_rows)} master rows, {len(alerts)} alerts generated"
    }

# ==========================================
# REUSABLE S3 ACQUISITION HANDLERS
# ==========================================

def fetch_previous_master_data():
    try:
        file_obj = s3.get_object(Bucket=BUCKET_NAME, Key=MASTER_FILE_KEY)
        content = file_obj["Body"].read().decode("utf-8-sig", errors="ignore")
        lines = [line for line in content.splitlines() if line.strip()]
        if len(lines) <= 1:
            return None
        return list(csv.DictReader(lines))
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        print(f"Error fetching previous data: {str(e)}")
        return None

def create_empty_master():
    output = StringIO()
    fieldnames = [
        "record_id", "source_file", "file_upload_date", "load_timestamp",
        "order_id", "order_date", "region", "product", "category",
        "quantity", "price", "discount", "sales", "profit", "shipping_cost"
    ]
    csv.DictWriter(output, fieldnames=fieldnames).writeheader()
    s3.put_object(Bucket=BUCKET_NAME, Key=MASTER_FILE_KEY, Body=output.getvalue(), ContentType="text/csv")

def create_empty_alerts():
    output = StringIO()
    fieldnames = [
        "alert_id", "alert_date", "current_date", "previous_date", "region", "product",
        "category", "current_revenue", "previous_revenue", "impact_amount",
        "change_percent", "severity", "status"
    ]
    csv.DictWriter(output, fieldnames=fieldnames).writeheader()
    s3.put_object(Bucket=BUCKET_NAME, Key=ALERT_FILE_KEY, Body=output.getvalue(), ContentType="text/csv")