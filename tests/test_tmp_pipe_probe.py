"""TEMPORARY measurement for #188 (removed before review): every FILE_PIPE_LOCAL_INFORMATION
field from the write handle of an anonymous pipe, in five states. Fails on purpose so CI
prints the table."""

import ctypes
import os
import threading
import time

import pytest

pytestmark = pytest.mark.windows

FIELDS = (
    "NamedPipeType",
    "NamedPipeConfiguration",
    "MaximumInstances",
    "CurrentInstances",
    "InboundQuota",
    "ReadDataAvailable",
    "OutboundQuota",
    "WriteQuotaAvailable",
    "NamedPipeState",
    "NamedPipeEnd",
)


def info(fd):
    import msvcrt
    from ctypes import wintypes

    class Iosb(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class Info(ctypes.Structure):
        _fields_ = [(n, wintypes.ULONG) for n in FIELDS]

    q = ctypes.WinDLL("ntdll").NtQueryInformationFile
    q.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Iosb),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    q.restype = ctypes.c_long
    iosb, i = Iosb(), Info()
    st = q(msvcrt.get_osfhandle(fd), ctypes.byref(iosb), ctypes.byref(i), ctypes.sizeof(i), 24)
    return {"status": hex(st & 0xFFFFFFFF), **{n: getattr(i, n) for n in FIELDS}}


def timed_write(fd, size, within=2.0):
    done = []

    def w():
        t = time.monotonic()
        os.write(fd, b"w" * size)
        done.append(round(time.monotonic() - t, 4))

    th = threading.Thread(target=w, daemon=True)
    th.start()
    th.join(within)
    return f"completed in {done[0]} s" if done else f"BLOCKED > {within} s"


def pending_read(fd, size):
    got = []
    th = threading.Thread(target=lambda: got.append(os.read(fd, size)), daemon=True)
    th.start()
    time.sleep(0.5)
    return th, got


def test_measure_pipe_fields():
    rows = []
    # (i) fresh
    r, w = os.pipe()
    rows.append(("i fresh", info(w), "", ""))
    os.close(r), os.close(w)
    # (ii) N bytes buffered, no reader
    r, w = os.pipe()
    os.write(w, b"x" * 1000)
    rows.append(("ii 1000 buffered", info(w), "read end: " + str(info(r)), ""))
    os.close(r), os.close(w)
    # (iii) pending large read, nothing buffered
    for size in (65536, 8192, 2048):
        r, w = os.pipe()
        th, got = pending_read(r, size)
        before = info(w)
        res = timed_write(w, 300)
        th.join(2)
        after = info(w)
        rows.append(
            (
                f"iii pending read {size}",
                before,
                f"300-byte write: {res}; reader got {len(got[0]) if got else None}",
                "after: " + str(after),
            )
        )
        os.close(r), os.close(w)
    # (iv) full pipe
    r, w = os.pipe()
    room = info(w)["WriteQuotaAvailable"]
    os.write(w, b"x" * room)
    rows.append(("iv full", info(w), "", ""))
    # (iv b) full pipe then a pending read would complete at once; skip
    os.close(r), os.close(w)
    # (v) pending 1-byte read, nothing buffered
    r, w = os.pipe()
    th, got = pending_read(r, 1)
    before = info(w)
    res = timed_write(w, 300)
    th.join(2)
    rows.append(
        (
            "v pending read 1",
            before,
            f"300-byte write: {res}; reader got {len(got[0]) if got else None}",
            "after: " + str(info(w)),
        )
    )
    os.close(r), os.close(w)
    # (vi) 1000 buffered, then a pending large read cannot exist (it completes); 3000 buffered + write 300 with quota
    r, w = os.pipe()
    os.write(w, b"x" * 3000)
    rows.append(
        (
            "vi 3000 buffered",
            info(w),
            "300-byte write: " + timed_write(w, 300),
            "after: " + str(info(w)),
        )
    )
    os.close(r), os.close(w)
    pytest.fail("\n" + "\n".join(f"MEASURE {a} | {b} | {c} | {d}" for a, b, c, d in rows))
