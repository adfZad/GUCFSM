"""
Test the AzureSqlPersistence layer against local SQL Server.
Run:   python test_persistence.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "field-service-bot", "app"))

os.environ["DB_CONNECTION_STRING"] = (
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=DESKTOP-5MEJ09S;"
    "Database=GUCFSM;"
    "Uid=sa;Pwd=Nokia@7610;"
    "TrustServerCertificate=yes;"
)

from persistence import AzureSqlPersistence


async def test():
    p = AzureSqlPersistence()
    passed = 0
    failed = 0

    def check(label, actual, expected):
        nonlocal passed, failed
        if actual == expected:
            print(f"  PASS: {label}")
            passed += 1
        else:
            print(f"  FAIL: {label}: got {actual!r}, expected {expected!r}")
            failed += 1

    # 1. User data
    print("-- User data --")
    await p.update_user_data(111, {"step": "PHONE", "phone": "971501234567"})
    await p.update_user_data(222, {"step": "UNITS", "unit": "Villa 12"})

    all_users = await p.get_user_data()
    check("user 111 step", all_users.get(111, {}).get("step"), "PHONE")
    check("user 222 unit", all_users.get(222, {}).get("unit"), "Villa 12")
    check("total users", len(all_users), 2)

    single = await p.refresh_user_data(111)
    check("refresh user 111", single.get("phone"), "971501234567")

    await p.drop_user_data(111)
    all_users = await p.get_user_data()
    check("drop user 111", 111 in all_users, False)

    # 2. Chat data
    print("")
    print("-- Chat data --")
    await p.update_chat_data(999, {"mode": "emergency"})
    chat = await p.get_chat_data()
    check("chat 999 mode", chat.get(999, {}).get("mode"), "emergency")

    # 3. Bot data
    print("")
    print("-- Bot data --")
    await p.update_bot_data({"version": "1.0", "compounds": ["Diamond", "Pearl"]})
    bot = await p.get_bot_data()
    check("bot version", bot.get("version"), "1.0")
    check("bot compounds", bot.get("compounds"), ["Diamond", "Pearl"])

    # 4. Conversations
    print("")
    print("-- Conversations --")
    await p.update_conversation("res_bot", (333, 333), 5)
    await p.update_conversation("res_bot", (444, 444), 2)
    conv = await p.get_conversations("res_bot")
    check("conv 333 state", conv.get((333, 333)), 5)
    check("conv 444 state", conv.get((444, 444)), 2)

    await p.update_conversation("res_bot", (333, 333), None)
    conv = await p.get_conversations("res_bot")
    check("conv 333 removed", (333, 333) in conv, False)

    # 5. Agent flow simulation
    print("")
    print("-- Agent flow simulation --")
    uid = 8580506857

    await p.update_user_data(uid, {"agent_uid": str(uid), "is_field_agent": True})
    await p.update_conversation("agent_bot", (uid, uid), 0)

    loaded = await p.get_user_data()
    check("agent uid persists", loaded[uid].get("agent_uid"), str(uid))
    check("is_field_agent persists", loaded[uid].get("is_field_agent"), True)

    loaded[uid]["compound"] = "Diamond Compound"
    await p.update_user_data(uid, loaded[uid])
    await p.update_conversation("agent_bot", (uid, uid), 1)

    loaded2 = await p.get_user_data()
    check("compound across invocation", loaded2[uid].get("compound"), "Diamond Compound")

    # Cleanup
    await p.drop_user_data(uid)
    await p.drop_user_data(222)
    await p.drop_chat_data(999)
    p._delete("bot", "bot")
    await p.update_conversation("res_bot", (444, 444), None)
    await p.update_conversation("agent_bot", (uid, uid), None)
    print("")
    print("  (test data cleaned up)")

    # Summary
    total = passed + failed
    print("")
    print(f"Results: {passed}/{total} passed")
    if failed:
        print(f"FAILED: {failed} test(s)")
        sys.exit(1)
    else:
        print("All tests passed!")

    await p.flush()


if __name__ == "__main__":
    asyncio.run(test())
