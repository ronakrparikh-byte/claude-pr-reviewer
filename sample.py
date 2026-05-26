# sample.py - intentionally has a few issues for Claude to catch
import os

def divide(a, b):
    return a / b  # no zero-division check

def read_file(path):
    f = open(path)  # file handle never closed
    return f.read()

password = "abc123"  # hardcoded credential