"""Datos de demo. Los proveedores están pensados para que el ranking sea interesante:
  1 Barato pero nuevo · 2 Equilibrado y veterano · 3 Rápido pero caro · 4 Disputas altas (se descarta)
  5 Sin KYC (se descarta) · 6 Sin capacidad (se descarta)
"""
from __future__ import annotations

import json
import sqlite3


def seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO customers (display_name, email, default_bank) VALUES ('Ana (demo)','ana@example.com','0102')")
    conn.execute("INSERT INTO merchants (name, paypal_merchant_id, category, delivery_type)"
                 " VALUES ('CursoOnline Demo','SANDBOX-MERCHANT','education','digital')")
    conn.execute("INSERT INTO merchants (name, paypal_merchant_id, category, delivery_type)"
                 " VALUES ('TiendaTech Demo','SANDBOX-MERCHANT','electronics','physical')")
    conn.executemany("INSERT INTO catalog_items (merchant_id, title, price_usd, billing_kind) VALUES (?,?,?,?)", [
        (1, "Curso de Python (licencia anual)", 29.0, "one_time"),
        (1, "Suscripción SaaS (1 año)", 120.0, "one_time"),
        (1, "Plan Mensual Demo (recurrente)", 15.0, "recurring"),
        (2, "Teclado mecánico", 45.0, "one_time"),
    ])
    # Bancos soportados: Venezuela, Mercantil, Provincial, Banesco, Banco Activo, Bancamiga, BNC, etc.
    banks = json.dumps(["0102", "0104", "0105", "0108", "0114", "0115", "0134", "0151", "0163", "0166", "0168", "0169", "0171", "0172", "0174", "0175", "0177", "0191"])
    rows = [
        # name, email, verified, kyc, status, done, disp, fail, settle_min, capacity, margin, fee
        ("Proveedor A (nuevo, barato)", "a@sandbox.test", 1, 2, "active", 3, 0, 0, 20, 500, 1.5, 0.0),
        ("Proveedor B (veterano)",      "b@sandbox.test", 1, 3, "active", 140, 3, 2, 12, 2000, 2.5, 0.0),
        ("Proveedor C (rápido, caro)",  "c@sandbox.test", 1, 3, "active", 60, 0, 1, 4, 1500, 5.0, 0.5),
        ("Proveedor D (disputas)",      "d@sandbox.test", 1, 3, "active", 40, 9, 3, 10, 800, 0.8, 0.0),
        ("Proveedor E (sin KYC)",       "e@sandbox.test", 1, 0, "active", 10, 0, 0, 10, 800, 1.0, 0.0),
        ("Proveedor F (sin saldo)",     "f@sandbox.test", 1, 3, "active", 90, 1, 1, 8, 5, 2.0, 0.0),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO providers (display_name, paypal_email, paypal_verified, kyc_level, status, completed_orders,"
            " disputed_orders, failed_orders, avg_settle_minutes, usd_capacity_available, margin_pct, fee_usd,"
            " accepts_banks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*r, banks))

    # Cuentas de Pago Móvil de los proveedores
    conn.executemany(
        "INSERT INTO provider_payout_accounts (provider_id, method, bank_code, phone, id_doc, holder_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            (1, "pago_movil", "0102", "0412-1112233", "V-20111222", "Alejandro Pérez (Prov A)", 1),
            (2, "pago_movil", "0102", "0414-9988776", "V-15888999", "Carlos Gómez (Prov B)", 1),
            (2, "pago_movil", "0134", "0414-9988776", "V-15888999", "Carlos Gómez (Prov B)", 1),
            (2, "pago_movil", "0171", "0414-9988776", "V-15888999", "Carlos Gómez (Prov B)", 1),
            (3, "pago_movil", "0102", "0416-5554433", "V-12333444", "Daniela Rivas (Prov C)", 1),
        ]
    )
    conn.commit()
