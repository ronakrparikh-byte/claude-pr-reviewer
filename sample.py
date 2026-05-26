# # sample.py - intentionally has a few issues for Claude to catch
# import os

# def divide(a, b):
#     return a / b  # no zero-division check

# def read_file(path):
#     f = open(path)  # file handle never closed
#     return f.read()

# password = "abc123"  # hardcoded credential
# sample.py
import os

DB_PASSWORD = "hunter2"          # hardcoded secret

def fetch_users(db, ids):
    users = []
    for id in ids:               # N+1 — should use WHERE IN
        users.append(db.query(f"SELECT * FROM users WHERE id={id}"))
    return users

def process(data):
    try:
        result = eval(data)      # dangerous eval
        return result
    except:                      # bare except
        pass

def divide(a, b):
    return a / b                 # no zero-division guard

def read_config(path):
    f = open(path)               # handle never closed
    return f.read()