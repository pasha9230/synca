from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from .catalog import normalize_model
from .models import PublishedItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id INTEGER NOT NULL DEFAULT 1, brand_key TEXT NOT NULL,
    season TEXT NOT NULL, size_class TEXT NOT NULL, markdown_pct REAL NOT NULL DEFAULT 0, discount_pct REAL NOT NULL DEFAULT 0,
    bonus_pct REAL NOT NULL DEFAULT 0, monthly_pct REAL NOT NULL DEFAULT 0, valid_from TEXT NOT NULL, created TEXT NOT NULL,
    author TEXT NOT NULL, UNIQUE (supplier_id, brand_key, season, size_class, valid_from));
CREATE TABLE IF NOT EXISTS sale (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, goods_id TEXT NOT NULL, brand_key TEXT NOT NULL, season TEXT NOT NULL,
    diameter INTEGER NOT NULL, name TEXT NOT NULL, ad_kind TEXT NOT NULL, pieces INTEGER NOT NULL, price_total REAL NOT NULL,
    sale_total REAL NOT NULL, cost_total REAL NOT NULL, bonus_total REAL NOT NULL, monthly_total REAL NOT NULL, fee_total REAL NOT NULL,
    promo_total REAL NOT NULL, margin_total REAL NOT NULL, account_id INTEGER, point TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual', created TEXT NOT NULL, author TEXT NOT NULL, reversed_at TEXT, reversed_by TEXT, point_id INTEGER);
CREATE INDEX IF NOT EXISTS sale_day ON sale (day);
CREATE TABLE IF NOT EXISTS order_rate (
    name TEXT NOT NULL, valid_from TEXT NOT NULL, value REAL NOT NULL, created TEXT NOT NULL, author TEXT NOT NULL,
    PRIMARY KEY (name, valid_from));
CREATE TABLE IF NOT EXISTS economics_snapshot (
    day TEXT NOT NULL, brand_key TEXT NOT NULL, ads INTEGER NOT NULL, stock INTEGER NOT NULL, profit REAL NOT NULL,
    sale_total REAL NOT NULL, price_total REAL NOT NULL, no_terms INTEGER NOT NULL, PRIMARY KEY (day, brand_key));
CREATE TABLE IF NOT EXISTS published_state (
    account_id INTEGER NOT NULL DEFAULT 1, goods_id TEXT NOT NULL, price INTEGER NOT NULL, base_price REAL NOT NULL,
    point TEXT NOT NULL, location TEXT NOT NULL DEFAULT 'supplier', first_hold TEXT, snapshot TEXT NOT NULL,
    PRIMARY KEY (account_id, goods_id)
);
CREATE TABLE IF NOT EXISTS suppressed (
    account_id INTEGER NOT NULL DEFAULT 1, goods_id TEXT NOT NULL, PRIMARY KEY (account_id, goods_id)
);
CREATE TABLE IF NOT EXISTS pending_state (
    account_id INTEGER PRIMARY KEY, feed_sha TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS known_point (goods_id TEXT PRIMARY KEY, point TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mapping (
    avito_brand TEXT NOT NULL, model_norm TEXT NOT NULL, avito_model TEXT NOT NULL,
    PRIMARY KEY (avito_brand, model_norm)
);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, dry_run INTEGER NOT NULL,
    input_sha TEXT NOT NULL, summary TEXT NOT NULL, account_id INTEGER
);
CREATE TABLE IF NOT EXISTS settings_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, author TEXT NOT NULL,
    note TEXT NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings_active (id INTEGER PRIMARY KEY CHECK (id = 1), version_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS draft (
    id INTEGER PRIMARY KEY AUTOINCREMENT, base_version INTEGER NOT NULL, changes TEXT NOT NULL,
    created TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, author TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_action ON audit(action, id);          -- the client card reads its events by action
CREATE TABLE IF NOT EXISTS brand_map (brand_key TEXT PRIMARY KEY, avito_brand TEXT NOT NULL);
-- the former name of an Avito model (the owner 2026-10-24, task 2: «Ikon Autograph Ice 9 — преемник Nokian Hakkapeliitta 9»); only the
-- description variable {old_model} reads it, the catalog never shows it
CREATE TABLE IF NOT EXISTS old_model_name (
    avito_brand TEXT NOT NULL, avito_model TEXT NOT NULL, old_name TEXT NOT NULL, updated TEXT NOT NULL, author TEXT NOT NULL,
    PRIMARY KEY (avito_brand, avito_model)
);
CREATE TABLE IF NOT EXISTS model_synonym (                            -- tire_match: one model under two names (rows are only added)
    brand TEXT NOT NULL, alias TEXT NOT NULL, canonical TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (brand, alias)
);
CREATE TABLE IF NOT EXISTS tire_pair (                                -- tire_match: one tire at two suppliers, as once found (rows are only added)
    code_a TEXT NOT NULL, code_b TEXT NOT NULL, identity_a TEXT NOT NULL, identity_b TEXT NOT NULL, found_at TEXT NOT NULL,
    PRIMARY KEY (code_a, code_b)
);
CREATE TABLE IF NOT EXISTS tire_match_switch (                        -- tire_match: an ad of a pair went over to another supplier (rows are only added)
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, ad_id TEXT NOT NULL, from_code TEXT NOT NULL, to_code TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tire_match_switch_ad ON tire_match_switch (account_id, ad_id);
CREATE TABLE IF NOT EXISTS tire_pair_forget (                         -- the owner: a remembered pair is (forgotten=1) or is again (0) out of the
    id INTEGER PRIMARY KEY AUTOINCREMENT, code_a TEXT NOT NULL, code_b TEXT NOT NULL,       -- memory; rows are only added, the last one of a
    forgotten INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL                      -- pair counts (tire_pair is never changed)
);
CREATE INDEX IF NOT EXISTS tire_pair_forget_pair ON tire_pair_forget (code_a, code_b, id);
CREATE TABLE IF NOT EXISTS tire_not_pair (                           -- the owner: two positions are not one tire (apart=1), never merged
    id INTEGER PRIMARY KEY AUTOINCREMENT, code_a TEXT NOT NULL, code_b TEXT NOT NULL,       -- even with both in the unload, or they may be
    apart INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL                          -- again (0); rows are only added, the last counts
);
CREATE INDEX IF NOT EXISTS tire_not_pair_pair ON tire_not_pair (code_a, code_b, id);
CREATE TABLE IF NOT EXISTS seen_goods (
    goods_id TEXT PRIMARY KEY, brand_key TEXT NOT NULL, model_raw TEXT NOT NULL, name TEXT NOT NULL,
    first_run INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_goods_first_run ON seen_goods(first_run);
CREATE TABLE IF NOT EXISTS seen_brand (brand_key TEXT PRIMARY KEY, first_run INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS panel_user (
    login TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS panel_user_role (
    login TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'owner', active INTEGER NOT NULL DEFAULT 1, point_id INTEGER,
    display_name TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS order_handoff (
    order_id INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, point_id INTEGER, sender TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
    need TEXT NOT NULL DEFAULT '', voice INTEGER NOT NULL DEFAULT 0, responsible TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, overdue_at TEXT
);
CREATE TABLE IF NOT EXISTS order_handoff_status (
    order_id INTEGER NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '', PRIMARY KEY (order_id, status)
);
CREATE TABLE IF NOT EXISTS order_handoff_source (
    order_id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'vk', chat_row INTEGER, call_id TEXT NOT NULL DEFAULT '', size TEXT NOT NULL DEFAULT '',
    season TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '', taken_at TEXT, taken_by TEXT NOT NULL DEFAULT '',
    taken_how TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS order_handoff_source_chat ON order_handoff_source (chat_row);
CREATE TABLE IF NOT EXISTS client (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '', merged_into INTEGER
);
CREATE TABLE IF NOT EXISTS client_phone (phone TEXT PRIMARY KEY, client_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS client_phone_client ON client_phone (client_id);
CREATE TABLE IF NOT EXISTS client_peer (account_id INTEGER NOT NULL, peer_id TEXT NOT NULL, client_id INTEGER NOT NULL, PRIMARY KEY (account_id, peer_id));
CREATE TABLE IF NOT EXISTS client_order (order_id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, how TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS client_order_client ON client_order (client_id);
CREATE TABLE IF NOT EXISTS order_oth_client (order_id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, how TEXT NOT NULL DEFAULT '',
    linked_at TEXT NOT NULL DEFAULT '', detached_at TEXT NOT NULL, detached_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS oth_return (item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, qty INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS oth_return_order ON oth_return (order_id);
-- the return of the goods of «На ОТХ» to the supplier (the owner 2026-10-29): agreed with the supplier for an odd date (the owner, the
-- partner), then the point signs the paper act МХ-3 and gives the tyres (order_oth.agree / sign_act)
CREATE TABLE IF NOT EXISTS oth_return_plan (order_id INTEGER PRIMARY KEY, agreed_at TEXT NOT NULL, agreed_by TEXT NOT NULL, return_date TEXT NOT NULL,
    signed_at TEXT, signed_by TEXT NOT NULL DEFAULT '');
-- «Взять с ОТХ» (oth_sale, the owner 2026-10-29): the position item_id of a new order is `qty` tyres taken from the position from_item of the
-- order from_order standing on «На ОТХ» (its quantity was lowered by them). sale_id: its sale once written (counted at the purchase of the old
-- order, never at WebMIM's price of the new one). undone_at: given back by «Вернуть на ОТХ» (the row stays: the Google table follows it).
CREATE TABLE IF NOT EXISTS order_item_oth (item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, from_item INTEGER NOT NULL, from_order INTEGER NOT NULL,
    qty INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL, sale_id INTEGER, undone_at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS order_item_oth_from ON order_item_oth (from_order);
-- «Объединить заказы» (order_merge, the owner 2026-10-30): a merge of sources into a new order; each source as it stood (for «Разъединить»);
-- each position given (oth: through order_item_oth, the source stood on «На ОТХ»)
CREATE TABLE IF NOT EXISTS order_merge (id INTEGER PRIMARY KEY AUTOINCREMENT, into_order INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL,
    undone_at TEXT NOT NULL DEFAULT '', undone_by TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS order_merge_into ON order_merge (into_order);
CREATE TABLE IF NOT EXISTS order_merge_source (merge_id INTEGER NOT NULL, order_id INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0,
    ref INTEGER NOT NULL DEFAULT 0, stage_id INTEGER NOT NULL, entered_at TEXT NOT NULL DEFAULT '', cancel_reason TEXT NOT NULL DEFAULT '',
    reserve_until TEXT NOT NULL DEFAULT '', chats TEXT NOT NULL DEFAULT '[]', calls TEXT NOT NULL DEFAULT '[]', callbacks TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (merge_id, order_id));
CREATE INDEX IF NOT EXISTS order_merge_source_order ON order_merge_source (order_id);
CREATE TABLE IF NOT EXISTS order_merge_item (item_id INTEGER PRIMARY KEY, merge_id INTEGER NOT NULL, from_item INTEGER NOT NULL,
    from_order INTEGER NOT NULL, qty INTEGER NOT NULL, oth INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS order_merge_item_from ON order_merge_item (from_order);
CREATE TABLE IF NOT EXISTS client_chat (chat_row INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, how TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS client_chat_client ON client_chat (client_id);
CREATE TABLE IF NOT EXISTS avito_call (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, call_id INTEGER NOT NULL, call_at TEXT NOT NULL,
    buyer_phone TEXT NOT NULL DEFAULT '', seller_phone TEXT NOT NULL DEFAULT '', virtual_phone TEXT NOT NULL DEFAULT '', item_id TEXT NOT NULL DEFAULT '',
    talk_seconds INTEGER NOT NULL DEFAULT 0, wait_seconds INTEGER NOT NULL DEFAULT 0, answered INTEGER NOT NULL DEFAULT 0, point_id INTEGER,
    order_id INTEGER, record_state TEXT NOT NULL DEFAULT '', record_file TEXT NOT NULL DEFAULT '', record_size INTEGER NOT NULL DEFAULT 0,
    record_until TEXT NOT NULL DEFAULT '', record_tries INTEGER NOT NULL DEFAULT 0, record_tried_at TEXT NOT NULL DEFAULT '',
    record_deleted_at TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL DEFAULT '', UNIQUE (account_id, call_id)
);
CREATE INDEX IF NOT EXISTS avito_call_at ON avito_call (call_at);
CREATE INDEX IF NOT EXISTS avito_call_phone ON avito_call (buyer_phone);
CREATE INDEX IF NOT EXISTS avito_call_record ON avito_call (record_state);
CREATE TABLE IF NOT EXISTS client_call (call_row INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, how TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS client_call_client ON client_call (client_id);
-- CRM (the owner 2026-10-23): the client's tags typed by hand («Прагматик», free ones; «передан точкой» is read from the handoffs, never kept)
-- and the pairs a person said are not one client (never proposed to join again; a < b)
CREATE TABLE IF NOT EXISTS client_tag (client_id INTEGER NOT NULL, tag TEXT NOT NULL, at TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', PRIMARY KEY (client_id, tag));
CREATE INDEX IF NOT EXISTS client_tag_tag ON client_tag (tag);
CREATE TABLE IF NOT EXISTS client_pair_skip (a INTEGER NOT NULL, b INTEGER NOT NULL, at TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS panel_invite (
    token_hash TEXT PRIMARY KEY, role TEXT NOT NULL, point_id INTEGER, display_name TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
    expires TEXT NOT NULL, created_by TEXT NOT NULL, used_at TEXT, used_by TEXT, revoked_at TEXT
);
-- the conditions of a client (a buyer; the owner 2026-10-29): subject '*' is the role's (the default), else a login. The price is the purchase
-- + the % of the brand by the diameter (pct_small up to R17, pct_large from R18); NULL: the role's. client_brand is the set of brands
-- (brands_mode 'own'; '*' always 'own') and the % of a brand (NULL: the subject's general one)
CREATE TABLE IF NOT EXISTS client_terms (
    subject TEXT PRIMARY KEY, pct_small REAL, pct_large REAL, brands_mode TEXT NOT NULL DEFAULT 'inherit',
    updated TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS client_brand (
    subject TEXT NOT NULL, brand_key TEXT NOT NULL, pct_small REAL, pct_large REAL, PRIMARY KEY (subject, brand_key)
);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempt_ip_at ON login_attempt (ip, at);      -- the count of one address on every sign-in
CREATE INDEX IF NOT EXISTS login_attempt_at ON login_attempt (at);             -- the count of all addresses and the cleanup of the day before
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, feed_token TEXT NOT NULL UNIQUE,
    live INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, description_template TEXT,
    chats_enabled INTEGER NOT NULL DEFAULT 0, feed_enabled INTEGER NOT NULL DEFAULT 1, feed_mode TEXT NOT NULL DEFAULT 'generated', chats_send INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS avito_address (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, position INTEGER NOT NULL,
    name TEXT NOT NULL, address_id TEXT NOT NULL, enabled INTEGER NOT NULL, share REAL NOT NULL,
    UNIQUE (account_id, address_id)
);
CREATE TABLE IF NOT EXISTS point_rule (
    account_id INTEGER NOT NULL, brand_key TEXT NOT NULL, season TEXT NOT NULL, address_id TEXT NOT NULL,
    PRIMARY KEY (account_id, brand_key, season)
);
CREATE TABLE IF NOT EXISTS supplier (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connection (
    kind TEXT NOT NULL, owner_id INTEGER NOT NULL DEFAULT 0, config TEXT NOT NULL DEFAULT '{}',
    last_check_at TEXT, last_check_status TEXT, PRIMARY KEY (kind, owner_id)
);
CREATE TABLE IF NOT EXISTS secret (
    kind TEXT NOT NULL, owner_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL,
    ciphertext TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (kind, owner_id, name)
);
CREATE TABLE IF NOT EXISTS avito_ad (
    account_id INTEGER NOT NULL, avito_id INTEGER NOT NULL, ad_id TEXT, title TEXT NOT NULL DEFAULT '', price INTEGER,
    status TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL, PRIMARY KEY (account_id, avito_id)
);
CREATE TABLE IF NOT EXISTS ad_stat (
    account_id INTEGER NOT NULL, avito_id INTEGER NOT NULL, day TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0, contacts INTEGER NOT NULL DEFAULT 0, favorites INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, avito_id, day)
);
CREATE TABLE IF NOT EXISTS retired_ad (
    account_id INTEGER NOT NULL, goods_id TEXT NOT NULL, ad_id TEXT NOT NULL, identity TEXT NOT NULL, removed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, goods_id)
);
CREATE TABLE IF NOT EXISTS ad_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, account_id INTEGER NOT NULL, goods_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '', event TEXT NOT NULL, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ad_event_at ON ad_event (at);
CREATE INDEX IF NOT EXISTS ad_event_goods ON ad_event (goods_id, at);
CREATE TABLE IF NOT EXISTS catalog_probe (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, source TEXT NOT NULL, items INTEGER NOT NULL,
    published INTEGER NOT NULL, present INTEGER NOT NULL, absent INTEGER NOT NULL, low INTEGER NOT NULL, waiting INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_item (
    goods_id TEXT PRIMARY KEY, supplier_id INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    last_stock INTEGER NOT NULL DEFAULT 0, last_price REAL NOT NULL DEFAULT 0, gone_since TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS catalog_item_gone ON catalog_item (gone_since);
CREATE TABLE IF NOT EXISTS order_stage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_id INTEGER NOT NULL DEFAULT 1, code TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#98A2B3', position INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'open',
    hint TEXT NOT NULL DEFAULT '', limit_hours INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS order_card (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_id INTEGER NOT NULL DEFAULT 1, stage_id INTEGER NOT NULL,
    client_name TEXT NOT NULL DEFAULT '', client_phone TEXT NOT NULL DEFAULT '', avito_chat TEXT NOT NULL DEFAULT '',
    point_id INTEGER, delivery_date TEXT NOT NULL DEFAULT '', delivery_time TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
    webmim_id TEXT NOT NULL DEFAULT '', webmim_number TEXT NOT NULL DEFAULT '', webmim_status TEXT NOT NULL DEFAULT '',
    cancel_reason TEXT NOT NULL DEFAULT '', return_kind TEXT NOT NULL DEFAULT '', return_reason TEXT NOT NULL DEFAULT '',
    return_amount REAL NOT NULL DEFAULT 0, sale_ids TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '', deal_kind TEXT NOT NULL DEFAULT 'webmim', transfer_number TEXT NOT NULL DEFAULT '',
    settled_point_at TEXT NOT NULL DEFAULT '', codes_written_off_at TEXT NOT NULL DEFAULT '', settled_supplier_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, created_by TEXT NOT NULL, entered_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, goods_id TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
    brand_key TEXT NOT NULL DEFAULT '', season TEXT NOT NULL DEFAULT 'all', diameter INTEGER NOT NULL DEFAULT 0,
    territory TEXT NOT NULL DEFAULT '', qty INTEGER NOT NULL, supplier_price REAL NOT NULL, sale_unit REAL NOT NULL,
    terms TEXT NOT NULL DEFAULT '{}', fee_pct REAL NOT NULL DEFAULT 0, promo_pct REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS order_item_order ON order_item (order_id);
CREATE TABLE IF NOT EXISTS order_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL,
    kind TEXT NOT NULL, text TEXT NOT NULL, level TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS order_event_order ON order_event (order_id, id);
CREATE TABLE IF NOT EXISTS order_event_seen (
    login TEXT NOT NULL, order_id INTEGER NOT NULL, last_id INTEGER NOT NULL, at TEXT NOT NULL, PRIMARY KEY (login, order_id)
);
CREATE TABLE IF NOT EXISTS order_event_ack (
    event_id INTEGER NOT NULL, login TEXT NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', PRIMARY KEY (event_id, login)
);
-- the orders of the supplier's WebMIM as its API last gave them (webmim_match.sync; read only, never sent back). server: test / prod.
-- create_date: ISO, Moscow; sum: of the positions (the order has none), NULL until they are read; items: JSON of the positions, NULL the same.
CREATE TABLE IF NOT EXISTS webmim_order (
    server TEXT NOT NULL, id TEXT NOT NULL, number TEXT NOT NULL DEFAULT '', status_code TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '', create_date TEXT NOT NULL DEFAULT '', external_number TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '', customer_phone_norm TEXT NOT NULL DEFAULT '', sum REAL, items TEXT,
    fingerprint TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL, items_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (server, id)
);
CREATE INDEX IF NOT EXISTS webmim_order_number ON webmim_order (server, number);
-- «Оформить в WebMIM» (webmim_create): one row per order of the panel. server: test / prod, where it is made; state: running (a press is at work),
-- unknown (the answer to the order was lost: it may be made), refused (WebMIM or a check said no, nothing made), partial (made, positions lacking),
-- done. first_at: the first press whose order may be made (the search by our number looks from it); webmim_id: the order made by the button.
CREATE TABLE IF NOT EXISTS webmim_create (
    order_id INTEGER PRIMARY KEY, server TEXT NOT NULL, state TEXT NOT NULL, first_at TEXT NOT NULL, started_at TEXT NOT NULL,
    author TEXT NOT NULL, webmim_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
-- the owner's Google table of orders (order_sheet): an order placed while the table is on. rows: what the table confirmed per row (mark →
-- columns; _missing: the owner deleted the row, _gone: the position left the order); seen_event: the last record of the order's journal looked
-- at; failing/next_at: the streak of failures and the next try; claim_until: a sender is at it.
CREATE TABLE IF NOT EXISTS sheet_order (
    order_id INTEGER PRIMARY KEY, placed_at TEXT NOT NULL, rows TEXT NOT NULL DEFAULT '{}', sent_at TEXT NOT NULL DEFAULT '',
    seen_event INTEGER NOT NULL DEFAULT 0, failing INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', next_at TEXT NOT NULL DEFAULT '',
    claim_until TEXT NOT NULL DEFAULT ''
);
-- the person's own settings of the panel (user_prefs): login → key → JSON (the columns of the list of orders, later the filters).
CREATE TABLE IF NOT EXISTS user_pref (
    login TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (login, key)
);
-- the client's cart (shop_cart.py, the owner 2026-10-30): only what and how many; the price and the stock are taken anew at the checkout
CREATE TABLE IF NOT EXISTS shop_cart (
    login TEXT NOT NULL, goods_id TEXT NOT NULL, qty INTEGER NOT NULL, added_at TEXT NOT NULL, PRIMARY KEY (login, goods_id)
);
-- the client's own contacts for the orders (asked at the first order, kept for the next)
CREATE TABLE IF NOT EXISTS client_profile (login TEXT PRIMARY KEY, phone TEXT NOT NULL, address TEXT NOT NULL, updated_at TEXT NOT NULL);
-- the orders a client placed from the catalog: «Мои заказы» shows only these, of this login
CREATE TABLE IF NOT EXISTS shop_order (order_id INTEGER PRIMARY KEY, login TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS shop_order_login ON shop_order (login);
CREATE TABLE IF NOT EXISTS photo (
    id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, ext TEXT NOT NULL, width INTEGER, height INTEGER, bytes INTEGER,
    source TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photo_set (
    key TEXT PRIMARY KEY, brand_key TEXT, model_key TEXT, season TEXT, studded INTEGER, size_class TEXT, ref_url TEXT,
    size_hint TEXT, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', rank INTEGER NOT NULL DEFAULT 0,
    brand_name TEXT NOT NULL DEFAULT '', model_name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS set_frame (
    id INTEGER PRIMARY KEY, set_key TEXT NOT NULL, kind TEXT NOT NULL, photo_id INTEGER NOT NULL, status TEXT NOT NULL,
    created TEXT NOT NULL, decided TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS set_frame_approved ON set_frame (set_key, kind) WHERE status = 'approved';
CREATE TABLE IF NOT EXISTS set_alias (alias_key TEXT PRIMARY KEY, set_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS image_job (
    id INTEGER PRIMARY KEY, set_key TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_photo (
    account_id INTEGER NOT NULL, source_photo_id INTEGER NOT NULL, kind TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '',
    photo_id INTEGER NOT NULL, PRIMARY KEY (account_id, source_photo_id, kind, variant)
);
CREATE TABLE IF NOT EXISTS order_alert_start (id INTEGER PRIMARY KEY CHECK (id = 1), last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS point (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, avito_address_id TEXT NOT NULL DEFAULT '',
    manager_name TEXT NOT NULL DEFAULT '', manager_phone TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '',
    webmim_address_id TEXT NOT NULL DEFAULT '', default_time TEXT NOT NULL DEFAULT '', bind_code TEXT NOT NULL DEFAULT '',
    vk_peer_id TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', hours TEXT NOT NULL DEFAULT '', map_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS order_field_value (
    order_id INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (order_id, field)
);
CREATE TABLE IF NOT EXISTS order_template (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, audience TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage_id INTEGER NOT NULL, trigger TEXT NOT NULL, hours INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL, template_id INTEGER, arg TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'ask',
    point_id INTEGER, position INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS order_rule_stage ON order_rule (stage_id, trigger);
CREATE TABLE IF NOT EXISTS order_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, rule_id INTEGER NOT NULL, entered_at TEXT NOT NULL,
    kind TEXT NOT NULL, arg TEXT NOT NULL DEFAULT '', stage_id INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, done_at TEXT, UNIQUE (order_id, rule_id, entered_at)
);
CREATE INDEX IF NOT EXISTS order_task_order ON order_task (order_id, status);
CREATE TABLE IF NOT EXISTS order_want (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, brand TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0, profile INTEGER NOT NULL DEFAULT 0, radius INTEGER NOT NULL DEFAULT 0,
    season TEXT NOT NULL DEFAULT '', studded TEXT NOT NULL DEFAULT '', qty INTEGER NOT NULL DEFAULT 1, code TEXT NOT NULL DEFAULT '',
    any_brand INTEGER NOT NULL DEFAULT 0, any_size INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_want_order ON order_want (order_id);
CREATE TABLE IF NOT EXISTS order_command (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage_id INTEGER NOT NULL, word TEXT NOT NULL, to_stage_id INTEGER NOT NULL,
    UNIQUE (stage_id, word)
);
CREATE TABLE IF NOT EXISTS order_command_log (
    message_id TEXT PRIMARY KEY, at TEXT NOT NULL, sender TEXT NOT NULL, word TEXT NOT NULL, order_id INTEGER, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE, at TEXT NOT NULL, point_id INTEGER, sender TEXT NOT NULL,
    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', answer TEXT NOT NULL DEFAULT '', answered_at TEXT
);
CREATE TABLE IF NOT EXISTS bot_answer (
    id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT NOT NULL, answer TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_send_log (
    token TEXT PRIMARY KEY, chat_row INTEGER NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS imported_ad (
    account_id INTEGER NOT NULL, position INTEGER NOT NULL, ad_id TEXT NOT NULL, raw TEXT NOT NULL, imported_at TEXT NOT NULL,
    PRIMARY KEY (account_id, ad_id)
);
CREATE TABLE IF NOT EXISTS avito_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, chat_id TEXT NOT NULL, item_id TEXT NOT NULL DEFAULT '',
    item_title TEXT NOT NULL DEFAULT '', item_url TEXT NOT NULL DEFAULT '', price_string TEXT NOT NULL DEFAULT '',
    peer_id TEXT NOT NULL DEFAULT '', peer_name TEXT NOT NULL DEFAULT '', last_text TEXT NOT NULL DEFAULT '', last_at TEXT NOT NULL DEFAULT '',
    last_dir TEXT NOT NULL DEFAULT '', unread INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open', status_at TEXT NOT NULL DEFAULT '',
    order_id INTEGER, unlinked INTEGER NOT NULL DEFAULT 0, history_ok INTEGER NOT NULL DEFAULT 1, updated_ts INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL DEFAULT 0, synced_at TEXT NOT NULL DEFAULT '', history_tried_at TEXT NOT NULL DEFAULT '', UNIQUE (account_id, chat_id)
);
CREATE INDEX IF NOT EXISTS avito_chat_last ON avito_chat (last_at);
CREATE INDEX IF NOT EXISTS avito_chat_order ON avito_chat (order_id);
CREATE TABLE IF NOT EXISTS avito_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_row INTEGER NOT NULL, message_id TEXT NOT NULL, at TEXT NOT NULL, ts INTEGER NOT NULL,
    direction TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text', text TEXT NOT NULL DEFAULT '', is_read INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_row, message_id)
);
CREATE TABLE IF NOT EXISTS avito_webhook (
    account_id INTEGER PRIMARY KEY, secret_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
    subscribed_at TEXT NOT NULL DEFAULT '', last_event_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS avito_webhook_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, message_id TEXT NOT NULL, chat_id TEXT NOT NULL, at TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '', UNIQUE (account_id, message_id)
);
CREATE TABLE IF NOT EXISTS vk_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, at TEXT NOT NULL, type TEXT NOT NULL,
    group_id TEXT NOT NULL, payload TEXT NOT NULL
);
-- a message of VK the bot took, once whichever way it came (vk_callback.claim): Callback or the backstop of vk_catchup
CREATE TABLE IF NOT EXISTS vk_message (
    message_id TEXT PRIMARY KEY, peer TEXT NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS share_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
    expires_at TEXT NOT NULL, revoked_at TEXT, show_price INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
    views INTEGER NOT NULL DEFAULT 0, last_view_at TEXT
);
CREATE TABLE IF NOT EXISTS deleted_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, record_id INTEGER NOT NULL, account_id INTEGER, chat_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL, data TEXT NOT NULL, deleted_at TEXT NOT NULL, deleted_by TEXT NOT NULL, restored_at TEXT, restored_by TEXT
);
CREATE INDEX IF NOT EXISTS deleted_record_chat ON deleted_record (kind, account_id, chat_id);
CREATE TABLE IF NOT EXISTS share_item (
    link_id INTEGER NOT NULL, pos INTEGER NOT NULL, data TEXT NOT NULL, photo TEXT, PRIMARY KEY (link_id, pos)
);
-- the inquiry of a client (inquiry.py): new → in work → ordered / refused; round_at starts the three days of the auto-refusal
CREATE TABLE IF NOT EXISTS client_inquiry (
    client_id INTEGER PRIMARY KEY, status TEXT NOT NULL, new_at TEXT NOT NULL DEFAULT '', round_at TEXT NOT NULL DEFAULT '',
    last_in_at TEXT NOT NULL DEFAULT '', worked_at TEXT NOT NULL DEFAULT '', ordered_at TEXT NOT NULL DEFAULT '', refused_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '', active_at TEXT NOT NULL DEFAULT '', looked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS client_inquiry_status ON client_inquiry (status);
-- «Перезвонить» (inquiry.py): a call to make at due_at by the assignee, shown in «Уведомления» when it is due
CREATE TABLE IF NOT EXISTS callback_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, order_id INTEGER, chat_row INTEGER, due_at TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, created_by TEXT NOT NULL,
    done_at TEXT, done_by TEXT NOT NULL DEFAULT '', cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS callback_task_open ON callback_task (done_at, cancelled_at, due_at);
-- the number in stock sent to Avito per ad (avito_stock.py): the last value and how it went; `before_*` is what Avito showed before our first send
CREATE TABLE IF NOT EXISTS avito_stock (
    account_id INTEGER NOT NULL, avito_id INTEGER NOT NULL, ad_id TEXT NOT NULL DEFAULT '', code TEXT NOT NULL DEFAULT '',
    quantity INTEGER, status TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT '',
    before_quantity INTEGER, before_at TEXT, PRIMARY KEY (account_id, avito_id)
);
-- the prices of the competitors on Avito (market.py, the plan price-parser): a snapshot per position and run, the listings it was made of
-- (no phones, no names of private sellers); the listings live 60 days, the snapshots a year. Nothing of the feed reads them.
CREATE TABLE IF NOT EXISTS market_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT, goods_id TEXT NOT NULL, run_at TEXT NOT NULL, slot TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 0, min REAL, q1 REAL, median REAL, q3 REAL, max REAL, confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS market_snapshot_goods ON market_snapshot (goods_id, id);
CREATE INDEX IF NOT EXISTS market_snapshot_run ON market_snapshot (run_at);
CREATE TABLE IF NOT EXISTS market_listing (
    id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INTEGER NOT NULL, goods_id TEXT NOT NULL, run_at TEXT NOT NULL,
    avito_id INTEGER NOT NULL, price REAL NOT NULL, lot INTEGER NOT NULL DEFAULT 1, per_unit REAL NOT NULL, title TEXT NOT NULL DEFAULT '',
    seller TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', listed_at TEXT NOT NULL DEFAULT '', year INTEGER, in_stock INTEGER,
    weight REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS market_listing_snapshot ON market_listing (snapshot_id);
CREATE INDEX IF NOT EXISTS market_listing_run ON market_listing (run_at);
"""


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    if column in columns:
        return
    try:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))
        conn.commit()
    except sqlite3.OperationalError as err:
        if "duplicate column" not in str(err):
            raise


def _rebuild_with_account(conn, table: str, columns: str) -> None:
    if "account_id" in {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}:
        return
    try:
        conn.executescript(
            "BEGIN IMMEDIATE; ALTER TABLE %(t)s RENAME TO %(t)s_old;" % {"t": table}
            + {"published_state": """CREATE TABLE published_state (
                account_id INTEGER NOT NULL DEFAULT 1, goods_id TEXT NOT NULL, price INTEGER NOT NULL,
                base_price REAL NOT NULL, point TEXT NOT NULL, location TEXT NOT NULL DEFAULT 'supplier',
                first_hold TEXT, snapshot TEXT NOT NULL, PRIMARY KEY (account_id, goods_id));""",
               "suppressed": """CREATE TABLE suppressed (
                account_id INTEGER NOT NULL DEFAULT 1, goods_id TEXT NOT NULL, PRIMARY KEY (account_id, goods_id));"""}[table]
            + "INSERT INTO %(t)s (account_id, %(c)s) SELECT 1, %(c)s FROM %(t)s_old; DROP TABLE %(t)s_old; COMMIT;"
            % {"t": table, "c": columns})
    except sqlite3.OperationalError:
        conn.rollback()
        if "account_id" not in {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}:
            raise


def _add_sale_point_id(conn) -> None:
    """2026-10-26: the sale is tied to its point by the id (a renamed point keeps its money in one line). Once, in one transaction: the column
    and its filling from the point with exactly the name written in the sale; the name stays as it was (history). A point named so later is
    never tied backwards: the filling runs only with the column's birth."""
    if "point_id" in {row[1] for row in conn.execute("PRAGMA table_info(sale)")}:
        return
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "point_id" not in {row[1] for row in conn.execute("PRAGMA table_info(sale)")}:      # another process may have done it meanwhile
            conn.execute("ALTER TABLE sale ADD COLUMN point_id INTEGER")
            conn.execute("UPDATE sale SET point_id = (SELECT p.id FROM point p WHERE p.name = sale.point) WHERE point != ''")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate(conn) -> None:
    _rebuild_with_account(conn, "published_state", "goods_id, price, base_price, point, location, first_hold, snapshot")
    _rebuild_with_account(conn, "suppressed", "goods_id")
    _add_column(conn, "published_state", "snapshot4", "TEXT")
    _add_column(conn, "avito_ad", "first_seen", "TEXT")
    _add_column(conn, "retired_ad", "snapshot", "TEXT")          # the last ad, from 2026-10-29: a position that goes to «На ОТХ» comes back on it
    if conn.execute("SELECT 1 FROM avito_ad WHERE first_seen IS NULL LIMIT 1").fetchone():          # an UPDATE that changes nothing still takes the write lock
        conn.execute("UPDATE avito_ad SET first_seen = seen_at WHERE first_seen IS NULL")
        conn.commit()
    _add_column(conn, "account", "description_template", "TEXT")
    if "own_photos" not in {row[1] for row in conn.execute("PRAGMA table_info(account)")}:
        # owner, 2026-10-27: own tire photos only in the accounts chosen for them; at first only the primary one.
        # The column and the primary's «on» land together: a crash in between must not leave the primary without photos.
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "own_photos" not in {row[1] for row in conn.execute("PRAGMA table_info(account)")}:   # another process was first
                conn.execute("ALTER TABLE account ADD COLUMN own_photos INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE account SET own_photos = 1 WHERE id = 1")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _add_column(conn, "photo_set", "positions", "INTEGER NOT NULL DEFAULT 0")     # in stock now: sizes of the model and pieces
    _add_column(conn, "photo_set", "stock", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "photo_set", "in_feed", "INTEGER NOT NULL DEFAULT 0")       # the owner sent this model's own photos to the feed
    _add_column(conn, "run", "account_id", "INTEGER")
    _add_column(conn, "account", "point_mode", "TEXT")
    _add_column(conn, "account", "point_rest", "TEXT")
    _add_column(conn, "account", "address_mode", "TEXT")
    _add_column(conn, "account", "price_delta", "REAL")
    _add_column(conn, "account", "min_stock", "INTEGER")
    _add_column(conn, "avito_address", "address_text", "TEXT")
    for column, ddl in (("source", "TEXT NOT NULL DEFAULT ''"), ("deal_kind", "TEXT NOT NULL DEFAULT 'webmim'"), ("transfer_number", "TEXT NOT NULL DEFAULT ''"),
                        ("settled_point_at", "TEXT NOT NULL DEFAULT ''"), ("codes_written_off_at", "TEXT NOT NULL DEFAULT ''"),
                        ("settled_supplier_at", "TEXT NOT NULL DEFAULT ''"), ("responsible", "TEXT NOT NULL DEFAULT ''"),
                        ("reserve_until", "TEXT NOT NULL DEFAULT ''"), ("special_terms", "TEXT NOT NULL DEFAULT ''"),
                        ("special_amount", "REAL NOT NULL DEFAULT 0"), ("receipt_needed", "INTEGER NOT NULL DEFAULT 0")):
        _add_column(conn, "order_card", column, ddl)
    _add_column(conn, "order_card", "supplier_number", "TEXT NOT NULL DEFAULT ''")   # the number of the order of 4 Точки, «Оформил вручную» (order_supplier)
    _add_column(conn, "order_item", "code", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "order_item", "purchase_manual", "INTEGER NOT NULL DEFAULT 0")   # 1: the purchase was typed by hand, WebMIM does not overwrite it
    _add_column(conn, "account", "chats_enabled", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "account", "feed_enabled", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "avito_chat", "history_tried_at", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "avito_chat", "responsible", "TEXT NOT NULL DEFAULT ''")          # who answers the chat (a panel login; '' = nobody)
    _add_column(conn, "avito_message", "media", "TEXT NOT NULL DEFAULT ''")          # the link to a client's photo on Avito's storage
    _add_column(conn, "account", "chats_send", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "chat_send_log", "error", "TEXT NOT NULL DEFAULT ''")          # Avito's answer to the last failed try of the send ('' = none)
    # 2026-10-25 send-retry: what the minute's sync needs to check an unknown send and to try a 402 again (chats.settle_sends)
    _add_column(conn, "chat_send_log", "text", "TEXT NOT NULL DEFAULT ''")           # the text sent (a photo: Avito's id of the picture); '' once settled
    _add_column(conn, "chat_send_log", "photo", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "chat_send_log", "tries", "INTEGER NOT NULL DEFAULT 0")        # «retry»: the 402s so far; «unknown»: the reads of the chat
    _add_column(conn, "chat_send_log", "next_at", "TEXT NOT NULL DEFAULT ''")        # when the sync takes it next ('' = not its business)
    _add_column(conn, "chat_send_log", "verdict", "TEXT NOT NULL DEFAULT ''")        # why an unknown send cannot be checked ('' = it can)
    _add_column(conn, "chat_send_log", "again_at", "TEXT NOT NULL DEFAULT ''")       # when the sync sent a lost one again by itself (once)
    _add_column(conn, "chat_send_log", "first_at", "TEXT NOT NULL DEFAULT ''")       # the first 402 of the send: the retries end RETRY_FOR after it
    _add_column(conn, "chat_send_log", "orig_at", "TEXT NOT NULL DEFAULT ''")        # the first start of the send, never moved: the chat is looked at from it
    _add_column(conn, "account", "feed_mode", "TEXT NOT NULL DEFAULT 'generated'")
    _add_column(conn, "order_task", "arg", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "order_task", "stage_id", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "order_event", "level", "TEXT NOT NULL DEFAULT ''")
    for column in ("address", "hours", "map_url"):                     # what the client is told on the arrival (order_templates)
        _add_column(conn, "point", column, "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "point", "avito_account_id", "INTEGER")          # the Avito account of the point: its manager sees its chats (none: no chat)
    _add_sale_point_id(conn)
    for column, ddl in (("car_brand", "TEXT NOT NULL DEFAULT ''"), ("car_model", "TEXT NOT NULL DEFAULT ''"), ("car_year", "INTEGER")):
        _add_column(conn, "client", column, ddl)                       # the client's car, typed by hand (CRM, the owner 2026-10-23)
    if not conn.execute("SELECT 1 FROM order_alert_start").fetchone():                      # once: the journal written before the alerts counts as seen
        conn.execute("INSERT OR IGNORE INTO order_alert_start (id, last_id) SELECT 1, COALESCE(MAX(id), 0) FROM order_event")
        conn.commit()
    for column in ("active_at", "looked_at"):                         # the inquiry: the last word of either side; the last look at its orders
        _add_column(conn, "client_inquiry", column, "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "panel_invite", "client_terms", "TEXT NOT NULL DEFAULT ''")     # the conditions a client's invitation puts in the profile (JSON)
    if not conn.execute("SELECT 1 FROM client_terms WHERE subject = '*'").fetchone():          # the role's conditions: empty (no price, no brand)
        conn.execute("INSERT OR IGNORE INTO client_terms (subject, brands_mode) VALUES ('*', 'own')")
        conn.commit()


BUSY_SECONDS = 15.0          # how long a connection waits for another one's write before «database is locked»


def open_db(path, timeout: float = BUSY_SECONDS) -> sqlite3.Connection:
    """Every connection to a base file (state.db and its copies) is opened here. The journal mode (delete or WAL) lives in the
    file and is switched by hand on the server, never here. synchronous is FULL in both modes, so a committed write survives a power
    cut (some builds of sqlite, Apple's, default to NORMAL in WAL)."""
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def connect(path, timeout: float = BUSY_SECONDS) -> sqlite3.Connection:
    conn = open_db(path, timeout=timeout)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


class StateError(Exception):
    pass


def _upsert(conn, item: PublishedItem, snapshot: str, account_id: int, snapshot4: Optional[str] = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO published_state "
        "(account_id, goods_id, price, base_price, point, location, first_hold, snapshot, snapshot4) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (account_id, item.goods_id, item.price, item.base_price, item.point, item.location,
         item.first_hold.isoformat() if item.first_hold else None, snapshot, snapshot4),
    )
    conn.execute("DELETE FROM suppressed WHERE account_id = ? AND goods_id = ?", (account_id, item.goods_id))


def _delete(conn, goods_id: str, suppress: bool, account_id: int) -> None:
    conn.execute("DELETE FROM published_state WHERE account_id = ? AND goods_id = ?", (account_id, goods_id))
    if suppress:
        conn.execute("INSERT OR IGNORE INTO suppressed (account_id, goods_id) VALUES (?,?)", (account_id, goods_id))


def upsert_published(conn, item: PublishedItem, snapshot: str, account_id: int = 1) -> None:
    _upsert(conn, item, snapshot, account_id)
    conn.commit()


def _parse_hold(text):
    """A hold is written as a date (older ones) or as a date with the time (waiting for the morning refill)."""
    if not text:
        return None
    return datetime.fromisoformat(text) if "T" in text else date.fromisoformat(text)


def load_published(conn, account_id: int = 1) -> Dict[str, PublishedItem]:
    rows = conn.execute(
        "SELECT goods_id, price, base_price, point, location, first_hold FROM published_state WHERE account_id = ?",
        (account_id,))
    return {
        r[0]: PublishedItem(r[0], r[1], r[2], r[3], r[4], _parse_hold(r[5]))
        for r in rows
    }


def get_snapshot(conn, goods_id: str, account_id: int = 1) -> Optional[str]:
    row = conn.execute(
        "SELECT snapshot FROM published_state WHERE account_id = ? AND goods_id = ?", (account_id, goods_id)).fetchone()
    return row[0] if row else None


def count_published_ads(conn, account_id: int = 1, twins: bool = True) -> int:
    """Ads of this account's last published feed: one per position plus, while «за 4 шт.» is on, one per twin."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(snapshot4 IS NOT NULL), 0) FROM published_state WHERE account_id = ?",
        (account_id,)).fetchone()
    return int(row[0]) + (int(row[1]) if twins else 0)


def get_snapshot4(conn, goods_id: str, account_id: int = 1) -> Optional[str]:
    row = conn.execute(
        "SELECT snapshot4 FROM published_state WHERE account_id = ? AND goods_id = ?", (account_id, goods_id)).fetchone()
    return row[0] if row else None


def delete_published(conn, goods_id: str, suppress: bool, account_id: int = 1) -> None:
    _delete(conn, goods_id, suppress, account_id)
    conn.commit()


def load_suppressed(conn, account_id: int = 1) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT goods_id FROM suppressed WHERE account_id = ?", (account_id,))}


def save_pending(conn, account_id: int, feed_bytes: bytes, items: List[dict]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pending_state (account_id, feed_sha, payload) VALUES (?,?,?)",
        (account_id, hashlib.sha256(feed_bytes).hexdigest(), json.dumps(items, ensure_ascii=False)))
    conn.commit()


def rebind_pending(conn, account_id: int, feed_bytes: bytes) -> None:
    """The plan of a cycle stays, but is now the plan of this (larger) feed: a mirror adds its own ads to the generated ones."""
    conn.execute("UPDATE pending_state SET feed_sha = ? WHERE account_id = ?", (hashlib.sha256(feed_bytes).hexdigest(), account_id))
    conn.commit()


def pending_matches(conn, account_id: int, feed_bytes: bytes) -> bool:
    row = conn.execute("SELECT feed_sha FROM pending_state WHERE account_id = ?", (account_id,)).fetchone()
    return row is not None and row[0] == hashlib.sha256(feed_bytes).hexdigest()


def has_pending(conn, account_id: int) -> bool:
    return conn.execute("SELECT 1 FROM pending_state WHERE account_id = ?", (account_id,)).fetchone() is not None


_TITLE = re.compile(r"<Title>(.*?)</Title>", re.S)


def _title(snapshot: Optional[str]) -> str:
    match = _TITLE.search(snapshot or "")
    return " ".join(html.unescape(match.group(1)).split())[:200] if match else ""


def record_event(conn, at: str, account_id: int, goods_id: str, name: str, event: str, reason: str = "") -> None:
    """The life of a position: published, waiting (for the morning refill or held by the guard), returned, removed. No commit here."""
    conn.execute("INSERT INTO ad_event (at, account_id, goods_id, name, event, reason) VALUES (?,?,?,?,?,?)",
                 (at, account_id, goods_id, name, event, reason))


def retire_ad(conn, account_id: int, goods_id: str, snapshot, at: str, ad_id: Optional[str] = None) -> None:
    """Remember the Id and the tire of an ad that leaves the feed, for good (owner, 2026-10-15): the same tire under a new supplier code takes
    over this Id, otherwise Avito blocks the new ad as a repeat and publishes the first one again. No commit here."""
    from . import same_tire
    try:
        key = same_tire.identity_of_ad(snapshot) if snapshot else None
        ad_id = ad_id or (ET.fromstring(snapshot).findtext("Id") if snapshot else None)    # the Id of the feed beats the one of an old snapshot
    except ET.ParseError:
        return
    if key is None or not ad_id:
        return
    conn.execute("INSERT OR REPLACE INTO retired_ad (account_id, goods_id, ad_id, identity, removed_at, snapshot) VALUES (?,?,?,?,?,?)",
                 (account_id, goods_id, ad_id, json.dumps(list(key), ensure_ascii=False), at, snapshot))


def retired_ad_of(conn, account_id: int, goods_id: str):
    """(Id, the last ad) of a position taken off this account's feed, or None: none, or taken off before the ads were kept (2026-10-29)."""
    row = conn.execute("SELECT ad_id, snapshot FROM retired_ad WHERE account_id = ? AND goods_id = ?", (account_id, goods_id)).fetchone()
    return (row[0], row[1]) if row and row[1] else None


def load_retired(conn, account_id: int, today) -> list:
    """[(goods_id, Id, identity, removed_at)] of every ad that was taken off the feed (they are kept for good)."""
    out = []
    for goods_id, ad_id, identity, removed_at in conn.execute(
            "SELECT goods_id, ad_id, identity, removed_at FROM retired_ad WHERE account_id = ?", (account_id,)):
        out.append((goods_id, ad_id, tuple(json.loads(identity)), removed_at))
    return out


def moscow_iso() -> str:
    """Moscow time whatever the server's zone (the life of positions and the retired ads have been kept in it from the start)."""
    return (datetime.utcnow() + timedelta(hours=3)).isoformat(timespec="seconds")


def commit_pending(conn, account_id: int, published_feed: bytes, at: Optional[str] = None) -> int:
    """Record as published what the just-promoted feed contains; refuses if the feed is not the one the plan was made for."""
    row = conn.execute("SELECT feed_sha, payload FROM pending_state WHERE account_id = ?", (account_id,)).fetchone()
    if row is None:
        raise StateError("Нет ожидающего состояния: сначала нужен цикл.")
    if hashlib.sha256(published_feed).hexdigest() != row[0]:
        raise StateError("Опубликованный фид не совпадает с последним циклом: состояние не записано.")
    at = at or moscow_iso()             # Moscow, as always: same_tire picks the latest retired ad by this stamp, so the clock never changes
    try:
        for item in json.loads(row[1]):
            action = item["action"]
            gid = item["goods_id"]
            before = conn.execute("SELECT first_hold, snapshot FROM published_state WHERE account_id = ? AND goods_id = ?",
                                  (account_id, gid)).fetchone()
            if action in ("publish", "update"):
                _upsert(conn, PublishedItem(gid, item["price"], item["base_price"], item["point"]),
                        item["snapshot"], account_id, item.get("snapshot4"))
                if before is None:
                    record_event(conn, at, account_id, gid, _title(item["snapshot"]), "published")
                    try:                                                                  # the Id is alive again: nothing left to take over
                        conn.execute("DELETE FROM retired_ad WHERE account_id = ? AND ad_id = ?",
                                     (account_id, ET.fromstring(item["snapshot"]).findtext("Id") or ""))
                    except ET.ParseError:
                        pass
                elif before[0]:
                    record_event(conn, at, account_id, gid, _title(item["snapshot"]), "returned")
            elif action == "remove":
                _delete(conn, gid, bool(item.get("suppressed")), account_id)
                if before is not None:
                    record_event(conn, at, account_id, gid, _title(before[1]), "removed", item.get("reason", ""))
                    retire_ad(conn, account_id, gid, before[1], at, item.get("ad_id"))
            elif action == "keep":
                if item.get("back_only"):
                    conn.execute("UPDATE published_state SET first_hold = ? WHERE account_id = ? AND goods_id = ?",
                                 (item.get("first_hold"), account_id, gid))
                else:
                    conn.execute(
                        "UPDATE published_state SET snapshot4 = ?, first_hold = ? WHERE account_id = ? AND goods_id = ?",
                        (item.get("snapshot4"), item.get("first_hold"), account_id, gid))       # a position that is back is no longer waiting
                if before is not None and before[0]:
                    record_event(conn, at, account_id, gid, _title(before[1]), "returned")
            elif action == "hold":
                conn.execute(
                    "UPDATE published_state SET first_hold = ? WHERE account_id = ? AND goods_id = ?",
                    (item.get("first_hold"), account_id, gid))
                if before is not None and not before[0]:
                    record_event(conn, at, account_id, gid, _title(before[1]), "waiting", item.get("reason", ""))
        conn.execute("DELETE FROM pending_state WHERE account_id = ?", (account_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(json.loads(row[1]))


def load_known_points(conn) -> Dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT goods_id, point FROM known_point")}


def upsert_mapping(conn, avito_brand: str, model_raw: str, avito_model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO mapping VALUES (?,?,?)", (avito_brand, normalize_model(model_raw), avito_model)
    )
    conn.execute("DELETE FROM app_state WHERE key = 'nav_pending_pairs'")
    conn.commit()


def load_confirmed_mappings(conn) -> Dict[Tuple[str, str], str]:
    return {(r[0], r[1]): r[2] for r in conn.execute("SELECT avito_brand, model_norm, avito_model FROM mapping")}


def save_run(conn, started: str, dry_run: bool, input_sha: str, summary: str, account_id: Optional[int] = None) -> int:
    cur = conn.execute(
        "INSERT INTO run (started, dry_run, input_sha, summary, account_id) VALUES (?,?,?,?,?)",
        (started, 1 if dry_run else 0, input_sha, summary, account_id),
    )
    conn.execute("DELETE FROM app_state WHERE key = 'nav_pending_pairs'")
    conn.commit()
    return cur.lastrowid


def write_audit(conn, at: str, author: str, action: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
        (at, author, action, json.dumps(detail, ensure_ascii=False)),
    )
    conn.commit()


SYSTEM_AUTHORS = ("system", "cli", "server")


def list_audit(conn, limit: int = 50, kind: str = "", author: str = "") -> List[dict]:
    """The journal, newest first. kind: "mine" (the given author), "system" (the service itself), "errors" (alerts)."""
    where, args = "", []
    if kind == "mine":
        where, args = "WHERE author = ?", [author]
    elif kind == "system":
        where, args = "WHERE author IN (%s) OR action IN ('alert', 'notice')" % ",".join("?" * len(SYSTEM_AUTHORS)), list(SYSTEM_AUTHORS)
    elif kind == "errors":
        where = "WHERE action = 'alert'"
    rows = conn.execute("SELECT at, author, action, detail FROM audit %s ORDER BY id DESC LIMIT ?" % where, args + [limit])
    return [{"at": r[0], "author": r[1], "action": r[2], "detail": json.loads(r[3])} for r in rows]


def _day_before(at: str) -> str:
    try:
        return (datetime.fromisoformat(at) - timedelta(days=1)).isoformat(timespec="seconds")
    except ValueError:
        return ""


def add_login_failure(conn, ip: str, at: str) -> None:
    conn.execute("INSERT INTO login_attempt (ip, at) VALUES (?,?)", (ip, at))
    conn.execute("DELETE FROM login_attempt WHERE at < ?", (_day_before(at),))
    conn.commit()


def count_login_failures(conn, ip: str, since: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM login_attempt WHERE ip = ? AND at >= ?", (ip, since)).fetchone()[0]


class SynonymConflict(ValueError):
    pass


def add_model_synonym(conn, brand: str, alias: str, canonical: str, author: str, at: str) -> bool:
    """One model of a brand under another name (tire_match): rows are only added, never changed or removed. The forms come normalized
    (tire_match.brand_form / model_form). True: added; False: the same pair is there already. Another name for a known alias raises."""
    if not (brand and alias and canonical) or alias == canonical:
        raise SynonymConflict("пустой бренд или модель, либо модель совпадает сама с собой")
    row = conn.execute("SELECT canonical FROM model_synonym WHERE brand = ? AND alias = ?", (brand, alias)).fetchone()
    if row is not None:
        if row[0] == canonical:
            return False
        raise SynonymConflict("у «%s» (%s) уже записано основное название «%s»" % (alias, brand, row[0]))
    if conn.execute("SELECT 1 FROM model_synonym WHERE brand = ? AND canonical = ?", (brand, alias)).fetchone():
        raise SynonymConflict("«%s» (%s) уже основное название другой модели: запишите пару к нему" % (alias, brand))
    chained = conn.execute("SELECT canonical FROM model_synonym WHERE brand = ? AND alias = ?", (brand, canonical)).fetchone()
    if chained is not None:                                   # one level only: A = B and B = C would leave A apart from C
        raise SynonymConflict("«%s» (%s) само записано как другое название «%s»: укажите «%s»" % (canonical, brand, chained[0], chained[0]))
    conn.execute("INSERT INTO model_synonym (brand, alias, canonical, author, created_at) VALUES (?,?,?,?,?)",
                 (brand, alias, canonical, author, at))
    conn.commit()
    return True


def load_model_synonyms(conn) -> Dict[Tuple[str, str], str]:
    """{(brand form, alias form): canonical form} of the model synonyms (tire_match)."""
    return {(r[0], r[1]): r[2] for r in conn.execute("SELECT brand, alias, canonical FROM model_synonym ORDER BY brand, alias")}


def add_tire_pairs(conn, pairs, at: str) -> int:
    """Remember the pairs of one tire at two suppliers [(code, code, identity of the first, identity of the second)] (tire_match): rows are
    only added, a pair that is there already keeps its first date. Returns how many were added."""
    added = 0
    for code_a, code_b, ident_a, ident_b in pairs:
        if code_a == code_b:
            continue
        if code_b < code_a:
            code_a, code_b, ident_a, ident_b = code_b, code_a, ident_b, ident_a
        added += conn.execute("INSERT OR IGNORE INTO tire_pair (code_a, code_b, identity_a, identity_b, found_at) VALUES (?,?,?,?,?)",
                              (code_a, code_b, ident_a, ident_b, at)).rowcount
    conn.commit()
    return added


def load_tire_pairs(conn) -> List[Tuple[str, str, str, str, str]]:
    """[(code_a, code_b, identity_a, identity_b, found_at)] of the remembered pairs (tire_match), by code."""
    return [tuple(r) for r in conn.execute("SELECT code_a, code_b, identity_a, identity_b, found_at FROM tire_pair ORDER BY code_a, code_b")]


def mark_tire_pair(conn, code_a: str, code_b: str, forgotten: bool, at: str, author: str) -> bool:
    """The owner forgets a remembered pair (`forgotten`) or brings it back (tire_match): a row is added, nothing is changed nor deleted; no
    commit (the journal of actions goes into the same one). False: no such remembered pair, or it is so already."""
    if code_b < code_a:
        code_a, code_b = code_b, code_a
    if not conn.execute("SELECT 1 FROM tire_pair WHERE code_a = ? AND code_b = ?", (code_a, code_b)).fetchone():
        return False
    if ((code_a, code_b) in forgotten_tire_pairs(conn)) == bool(forgotten):
        return False
    conn.execute("INSERT INTO tire_pair_forget (code_a, code_b, forgotten, at, author) VALUES (?,?,?,?,?)",
                 (code_a, code_b, 1 if forgotten else 0, at, author))
    return True


def forgotten_tire_pairs(conn) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """{(code_a, code_b): (at, author)} of the remembered pairs the owner has forgotten (the last mark of a pair counts)."""
    last: Dict[Tuple[str, str], Tuple[int, str, str]] = {}
    for code_a, code_b, forgotten, at, author in conn.execute(
            "SELECT code_a, code_b, forgotten, at, author FROM tire_pair_forget ORDER BY id"):
        last[(code_a, code_b)] = (forgotten, at, author)
    return {pair: (at, author) for pair, (forgotten, at, author) in last.items() if forgotten}


def mark_not_pair(conn, code_a: str, code_b: str, apart: bool, at: str, author: str) -> bool:
    """The owner says two positions are not one tire (`apart`) or takes it back (tire_match): a row is added, nothing is changed nor
    deleted; no commit (the journal of actions goes into the same one). False: it is so already."""
    if code_b < code_a:
        code_a, code_b = code_b, code_a
    if ((code_a, code_b) in not_tire_pairs(conn)) == bool(apart):
        return False
    conn.execute("INSERT INTO tire_not_pair (code_a, code_b, apart, at, author) VALUES (?,?,?,?,?)",
                 (code_a, code_b, 1 if apart else 0, at, author))
    return True


def not_tire_pairs(conn) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """{(code_a, code_b): (at, author)} of the two positions the owner said are not one tire (the last mark of a pair counts)."""
    last: Dict[Tuple[str, str], Tuple[int, str, str]] = {}
    for code_a, code_b, apart, at, author in conn.execute("SELECT code_a, code_b, apart, at, author FROM tire_not_pair ORDER BY id"):
        last[(code_a, code_b)] = (apart, at, author)
    return {pair: (at, author) for pair, (apart, at, author) in last.items() if apart}


def add_match_switch(conn, account_id: int, ad_id: str, from_code: str, to_code: str, at: str) -> None:
    """An ad of a pair went over to the code of another supplier (tire_match): rows are only added."""
    conn.execute("INSERT INTO tire_match_switch (account_id, ad_id, from_code, to_code, at) VALUES (?,?,?,?,?)",
                 (account_id, ad_id, from_code, to_code, at))
    conn.commit()


def last_match_switches(conn, account_id: int) -> Dict[str, Tuple[str, str, str]]:
    """{Id: (from code, to code, at)} of the last change of supplier of every ad of the account that had one (tire_match)."""
    out: Dict[str, Tuple[str, str, str]] = {}
    for ad_id, from_code, to_code, at in conn.execute(
            "SELECT ad_id, from_code, to_code, at FROM tire_match_switch WHERE account_id = ? ORDER BY at, id", (account_id,)):
        out[ad_id] = (from_code, to_code, at)
    return out


def upsert_brand_map(conn, brand_key: str, avito_brand: str) -> None:
    conn.execute("INSERT OR REPLACE INTO brand_map VALUES (?,?)", (brand_key, avito_brand))
    conn.execute("DELETE FROM app_state WHERE key = 'nav_pending_pairs'")
    conn.commit()


def load_brand_map(conn) -> Dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT brand_key, avito_brand FROM brand_map")}


def load_old_model_names(conn) -> Dict[Tuple[str, str], str]:
    """(Avito brand, Avito model) -> the former name of the model, for the description variable {old_model}."""
    return {(r[0], r[1]): r[2] for r in conn.execute("SELECT avito_brand, avito_model, old_name FROM old_model_name")}


def set_old_model_name(conn, avito_brand: str, avito_model: str, old_name: str, now: str, author: str, commit: bool = True) -> None:
    """Save the former name of an Avito model; an empty one removes it. The caller checks the brand, the model and the text
    (commit=False: the caller commits it together with its journal line)."""
    if old_name:
        conn.execute("INSERT OR REPLACE INTO old_model_name VALUES (?,?,?,?,?)", (avito_brand, avito_model, old_name, now, author))
    else:
        conn.execute("DELETE FROM old_model_name WHERE avito_brand = ? AND avito_model = ?", (avito_brand, avito_model))
    if commit:
        conn.commit()


def record_seen(conn, run_id: int, offers) -> None:
    for o in offers:
        conn.execute("INSERT OR IGNORE INTO seen_goods VALUES (?,?,?,?,?)",
                     (o.goods_id, o.brand_key, o.model_raw, o.name_orig or o.name, run_id))
        conn.execute("INSERT OR IGNORE INTO seen_brand VALUES (?,?)", (o.brand_key, run_id))
    conn.commit()


def last_run(conn):
    row = conn.execute(
        "SELECT id, started, dry_run, summary, account_id FROM run "
        "WHERE account_id IS NULL OR account_id = (SELECT MIN(id) FROM account) ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "started": row[1], "dry_run": bool(row[2]), "summary": json.loads(row[3]),
            "account_id": row[4]}


def new_positions(conn, run_id: int) -> dict:
    first = conn.execute("SELECT MIN(id) FROM run").fetchone()[0]
    goods = [
        {"goods_id": r[0], "brand_key": r[1], "model_raw": r[2], "name": r[3]}
        for r in conn.execute(
            "SELECT goods_id, brand_key, model_raw, name FROM seen_goods WHERE first_run = ? "
            "ORDER BY brand_key, name", (run_id,))
    ]
    brands = [r[0] for r in conn.execute(
        "SELECT brand_key FROM seen_brand WHERE first_run = ? ORDER BY brand_key", (run_id,))]
    return {"baseline": run_id == first, "goods": goods, "brands": brands}


NEW_MARKER_KEY = "new_seen_run"


def get_new_marker(conn) -> int:
    value = get_state(conn, NEW_MARKER_KEY)
    return int(value) if value and value.isdigit() else 0


def mark_new_seen(conn, run_id: int) -> None:
    set_state(conn, NEW_MARKER_KEY, str(int(run_id)))


def run_before(conn, started: str) -> int:
    """Id of the latest run that started before the given moment (0 when there is none)."""
    row = conn.execute("SELECT MAX(id) FROM run WHERE started < ?", (started,)).fetchone()
    return row[0] or 0


def new_since(conn, after_run: int) -> dict:
    """Goods and brands first seen after the given run, across all later runs; the very first run is only a baseline."""
    first = conn.execute("SELECT MIN(id) FROM run").fetchone()[0] or 0
    goods = [
        {"goods_id": r[0], "brand_key": r[1], "model_raw": r[2], "name": r[3], "seen_at": r[4]}
        for r in conn.execute(
            "SELECT g.goods_id, g.brand_key, g.model_raw, g.name, r.started FROM seen_goods g "
            "JOIN run r ON r.id = g.first_run WHERE g.first_run > ? AND g.first_run != ? "
            "ORDER BY g.first_run DESC, g.brand_key, g.name", (after_run, first))
    ]
    brands = [r[0] for r in conn.execute(
        "SELECT brand_key FROM seen_brand WHERE first_run > ? AND first_run != ? ORDER BY brand_key", (after_run, first))]
    return {"goods": goods, "brands": brands}


def count_new_since(conn, after_run: int) -> int:
    first = conn.execute("SELECT MIN(id) FROM run").fetchone()[0] or 0
    return conn.execute(
        "SELECT COUNT(*) FROM seen_goods WHERE first_run > ? AND first_run != ?", (after_run, first)).fetchone()[0]


def get_state(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (key, value))
    conn.commit()


def claim_state(conn, key: str, value: str, older_than: str) -> bool:
    """Writes value when the key is missing or its value is below older_than, in one statement: of two processes only one gets True."""
    cursor = conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                          "WHERE app_state.value < ?", (key, value, older_than))
    conn.commit()
    return cursor.rowcount == 1


def _epoch_key(login) -> str:
    return "session_epoch" if not login else "session_epoch:%s" % login


def get_session_epoch(conn, login=None) -> int:
    """Sessions of one user end together; login None is the owner of the shared password."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (_epoch_key(login),)).fetchone()
    return int(row[0]) if row else 0


def bump_session_epoch(conn, login=None) -> int:
    epoch = get_session_epoch(conn, login) + 1
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (_epoch_key(login), str(epoch)))
    conn.commit()
    return epoch
