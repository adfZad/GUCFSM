#!/usr/bin/env python3
"""Simulate a complete field service form flow."""
import sqlite3, json, os, re

DB = "/opt/data/field-service-bot/field_service.db"

def normalize_phone(phone):
    return re.sub(r'\D', '', phone)

def set_state(step, data):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR REPLACE INTO form_state (telegram_user_id, current_step, data) VALUES (?, ?, ?)",
                 ('sim_test', step, json.dumps(data)))
    conn.commit()
    conn.close()

def clear_state():
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM form_state WHERE telegram_user_id='sim_test'")
    conn.commit()
    conn.close()

clear_state()

print("=== FULL FORM FLOW SIMULATION ===\n")

# Step 1: Phone
set_state("phone", {})
user_input = "+974 5555 2345"
digits = normalize_phone(user_input)
conn = sqlite3.connect(DB)
row = conn.execute("SELECT phone_number, owner_name, units FROM master_units WHERE phone_number = ?", (digits,)).fetchone()
conn.close()
assert row, f"Phone {digits} not found!"
data = {"phone": row[0], "owner": row[1], "units": json.loads(row[2])}
print(f"1. Phone: '{user_input}' -> found {row[1]} ({len(data['units'])} units)")
set_state("units", data)

# Step 2: Unit
data["unit"] = data["units"][0]
print(f"2. Unit: selected '{data['unit']}'")
set_state("request_type", data)

# Step 3: Request type
data["request_type"] = "New Request"
print(f"3. Type: New Request")
set_state("category", data)

# Step 4: Category
data["category"] = "Maintenance"
print(f"4. Category: Maintenance")
set_state("service", data)

# Step 5: Service
data["service"] = "Plumbing"
print(f"5. Service: Plumbing")
set_state("description", data)

# Step 6: Description
data["description"] = "Leaking pipe under kitchen sink, water pooling on floor"
print(f"6. Issue: '{data['description'][:50]}...'")
set_state("photo", data)

# Step 7: Photo (skip)
data["photo_path"] = None
print(f"7. Photo: skipped")
set_state("confirm", data)

# Step 8: Write to DB
conn = sqlite3.connect(DB)
conn.execute("""
    INSERT INTO submissions (telegram_user_id, phone_number, unit, request_type, category, service, issue_description, photo_path)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""", ("sim_test", data["phone"], data["unit"], data["request_type"], data["category"], data["service"], data["description"], data.get("photo_path")))
conn.commit()
sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.close()
clear_state()

# Read back
conn = sqlite3.connect(DB)
row = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
conn.close()

print(f"\n=== SUBMISSION #{sid} -> DB ===")
print(f"  Phone:     {row[2]}")
print(f"  Unit:      {row[3]}")
print(f"  Type:      {row[4]}")
print(f"  Category:  {row[5]}")
print(f"  Service:   {row[6]}")
print(f"  Issue:     {row[7]}")
print(f"  Photo:     {row[8]}")
print(f"  Status:    {row[9]}")
print(f"  Time:      {row[10]}")
print(f"\nFULL FLOW PASSED - data written to DB")

# Clean up
conn = sqlite3.connect(DB)
conn.execute("DELETE FROM submissions WHERE telegram_user_id='sim_test'")
conn.commit()
conn.close()
