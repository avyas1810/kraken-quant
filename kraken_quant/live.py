"""Fail-closed Kraken spot execution, durable intents and reconciliation.

Real transport is disabled unless separate approval and evidence are supplied.
No funding endpoint exists. Exchange responses and secrets are never logged.
"""
from dataclasses import asdict, replace
from decimal import Decimal
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .data import canonical_json, utc_instant
from .operations import ProcessLock
from .risk import AccountSnapshot, InstrumentRules, RiskGateway, money, spot_only

READ = frozenset({'GetApiKeyInfo', 'Balance', 'OpenOrders', 'ClosedOrders', 'TradesHistory', 'TradeVolume'})
WRITE = frozenset({'AddOrder', 'CancelOrder', 'CancelAllOrdersAfter'})
PERMISSIONS = frozenset({'query-funds', 'query-open-trades', 'query-closed-trades', 'modify-trades', 'close-trades'})
TERMINAL = frozenset({'closed', 'canceled', 'expired', 'rejected'})


class NotSubmitted(PermissionError):
    """A final local guard prevented the wire submission."""


class ExchangeRejected(RuntimeError):
    """Explicit Kraken error response; no raw exchange content retained."""


class AmbiguousSubmission(RuntimeError):
    """Network/parse failure: exchange might have accepted the request."""


def check_trading_permissions(info, now):
    if not isinstance(info.get('permissions'), list) or set(info['permissions']) != PERMISSIONS:
        raise PermissionError('Trading key must have exactly approved read/order/cancel permissions; no funding')
    if any(money(info.get(k, 0)) for k in ('queryFrom', 'queryTo')):
        raise PermissionError('Restricted history prevents reconciliation')
    expiry = money(info.get('validUntil', 0))
    if expiry and expiry <= money(utc_instant(now).timestamp()):
        raise PermissionError('Trading key expired')
    return sorted(PERMISSIONS)


def approval_check(approval, evidence, *, fingerprint, now):
    """Operator-authored approval must bind the reviewed evidence bytes.

    This is an operator control, not a cryptographic third-party signature.
    Neither an example nor a CLI command generates approval automatically.
    """
    expected = hashlib.sha256(canonical_json(evidence)).hexdigest()
    if (approval.get('action') != 'AUTHORISE_KRAKEN_SPOT_LIVE_ORDERS'
            or not approval.get('operator') or approval.get('account_fingerprint') != fingerprint
            or approval.get('evidence_sha256') != expected
            or approval.get('candidate_sha256') != evidence.get('candidate_sha256')):
        raise PermissionError('Separate operator approval is missing or mismatched')
    if not utc_instant(approval['approved_at']) <= now < utc_instant(approval['expires_at']):
        raise PermissionError('Live approval inactive or expired')
    if utc_instant(approval['expires_at']) - utc_instant(approval['approved_at']) > pd.Timedelta(days=1):
        raise PermissionError('Live approval must expire within 24 hours')
    if (evidence.get('qualified') is not True or evidence.get('reliability_gate_passed') is not True
            or evidence.get('actual_daily_decisions', 0) < 30
            or evidence.get('simulated_order_lifecycles', 0) < 20
            or evidence.get('elapsed_seconds', 0) < 29 * 86400
            or evidence.get('completion_fraction', 0) < .995
            or evidence.get('fault_events') != 0 or evidence.get('missing_days') != 0
            or evidence.get('evidence_type') != 'prospective_public_feed'
            or any(not isinstance(evidence.get(k), str) or len(evidence[k]) != 64
                   for k in ('candidate_sha256', 'qualification_sha256', 'shadow_export_sha256'))):
        raise PermissionError('Reviewed qualification and genuine prospective evidence required')
    evidence_files_check(evidence)


def evidence_files_check(evidence):
    from .candidate import read_candidate, qualify_candidate
    directory = Path(evidence['artifact_directory'])
    if not directory.is_absolute():
        raise PermissionError('Absolute reviewed artifact directory required')
    paths = {}
    for prefix in ('candidate', 'qualification', 'shadow_export'):
        path = Path(evidence[prefix + '_file'])
        path = path if path.is_absolute() else directory / path
        key = 'candidate_file_sha256' if prefix == 'candidate' else prefix + '_sha256'
        if hashlib.sha256(path.read_bytes()).hexdigest() != evidence[key]:
            raise PermissionError('Reviewed evidence file hash mismatch')
        paths[prefix] = path
    candidate = read_candidate(paths['candidate'])
    if candidate['sha256'] != evidence['candidate_sha256']:
        raise PermissionError('Frozen candidate identity mismatch')
    shadow = json.loads(paths['shadow_export'].read_text(encoding='utf-8-sig'))
    report = json.loads(paths['qualification'].read_text(encoding='utf-8-sig'))
    computed = qualify_candidate(paths['candidate'], shadow)
    if report != computed or not computed['qualified_for_review']:
        raise PermissionError('Qualification report does not match reviewed prospective evidence')
    for field in ('actual_daily_decisions', 'simulated_order_lifecycles', 'candidate_sha256'):
        if shadow.get(field) != evidence.get(field):
            raise PermissionError('Prospective evidence summary mismatch')
    if shadow.get('source') != evidence.get('evidence_type') or shadow.get('reliability_passed') is not True:
        raise PermissionError('Genuine prospective source and reliability required')
    return computed


class KrakenTransport:
    """Authenticated REST allowlist; durable nonce, throttle, no HTTP retries.

    Use one DB/key pair and no other programs sharing the trading key.
    """
    simulated = False

    def __init__(self, database, key, secret, readonly_key, *, approval=None, evidence=None,
                 opener=urlopen, clock=lambda: pd.Timestamp.now(tz='UTC'), sleeper=time.sleep):
        if not key or not secret or not readonly_key or key == readonly_key:
            raise PermissionError('Separate dedicated read-only and trading keys required')
        self._key, self._secret, self._opener = key, secret, opener
        self.fingerprint = hashlib.sha256(key.encode()).hexdigest()
        self.approval, self.evidence, self.clock, self.sleeper = approval, evidence, clock, sleeper
        self.db = sqlite3.connect(str(database), isolation_level=None, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS live_nonce (id INTEGER PRIMARY KEY CHECK(id=1), n INTEGER NOT NULL)')
        self.last = 0.0
        self._public_markets = None
        self._markets_observed_at = None
        self._permissions_observed_at = None

    @classmethod
    def from_env(cls, database, path, **kwargs):
        from dotenv import dotenv_values
        values = dotenv_values(Path(path), interpolate=False)
        return cls(database, values.get('KRAKEN_TRADING_API_KEY'), values.get('KRAKEN_TRADING_API_SECRET'),
                   values.get('KRAKEN_READONLY_API_KEY'), **kwargs)

    def market(self, instrument):
        spot_only(instrument)
        now = utc_instant(self.clock())
        if (self._public_markets is None or self._markets_observed_at is None
                or not pd.Timedelta(0) <= now - self._markets_observed_at < pd.Timedelta(hours=1)):
            observed_at = now
            import ccxt
            client = ccxt.kraken({'enableRateLimit': True, 'timeout': 10000})
            try:
                markets = client.load_markets()
                if not pd.Timedelta(0) <= utc_instant(self.clock()) - observed_at <= pd.Timedelta(seconds=30):
                    raise RuntimeError('Public metadata request timed out')
                self._public_markets = markets
                self._markets_observed_at = observed_at
            except Exception:
                raise RuntimeError('Public instrument lookup failed') from None
        row = self._public_markets[instrument.symbol]
        info = row['info']
        return dict(pair=row['id'], base_id=info['base'], quote_id=info['quote'],
                    aliases=list({row['id'], instrument.symbol, info.get('altname', row['id'])}),
                    rules=InstrumentRules(money(10) ** -int(info['lot_decimals']),
                        money(info.get('tick_size', money(10) ** -int(info['pair_decimals']))),
                        info['ordermin'], info['costmin'], self._markets_observed_at, instrument, info.get('status','online')))

    def close(self):
        self.db.close()

    def _nonce(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT n FROM live_nonce WHERE id=1').fetchone()
            n = max(int(utc_instant(self.clock()).timestamp() * 1000), row[0] + 1 if row else 1)
            self.db.execute('INSERT OR REPLACE INTO live_nonce VALUES (1, ?)', (n,))
            self.db.execute('COMMIT')
            return n
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def request(self, endpoint, params=None, *, pre_send=None):
        if endpoint not in READ | WRITE:
            raise PermissionError('Endpoint not permitted')
        if endpoint in WRITE:
            if not self.approval or not self.evidence:
                raise PermissionError('Real execution disabled by default')
            approval_check(self.approval, self.evidence, fingerprint=self.fingerprint, now=utc_instant(self.clock()))
            if (self._permissions_observed_at is None
                    or not pd.Timedelta(0) <= utc_instant(self.clock()) - self._permissions_observed_at <= pd.Timedelta(seconds=60)):
                self.request('GetApiKeyInfo')
        self.sleeper(max(0, 1.1 - (time.monotonic() - self.last)))
        nonce = self._nonce()
        if endpoint in WRITE:
            approval_check(self.approval, self.evidence, fingerprint=self.fingerprint, now=utc_instant(self.clock()))
        if pre_send is not None:
            pre_send()
        payload = dict(params or {}, nonce=nonce)
        encoded = urlencode(payload).encode()
        path = '/0/private/' + endpoint
        try:
            digest = hashlib.sha256(str(nonce).encode() + encoded).digest()
            signature = base64.b64encode(hmac.new(base64.b64decode(self._secret), path.encode() + digest, hashlib.sha512).digest()).decode()
            request = Request('https://api.kraken.com' + path, data=encoded,
                              headers={'API-Key': self._key, 'API-Sign': signature, 'Content-Type': 'application/x-www-form-urlencoded'})
            self.last = time.monotonic()
            with self._opener(request, timeout=15) as response:
                body = json.loads(response.read(4_000_001))
        except Exception:
            raise AmbiguousSubmission('Kraken transport failed; do not retry order submission') from None
        if not isinstance(body, dict) or not isinstance(body.get('error'), list):
            raise AmbiguousSubmission('Invalid Kraken response')
        if body['error']:
            raise ExchangeRejected('Kraken explicitly rejected request')
        if not isinstance(body.get('result'), dict):
            raise AmbiguousSubmission('Missing Kraken result')
        if endpoint == 'GetApiKeyInfo':
            check_trading_permissions(body['result'], self.clock())
            self._permissions_observed_at = utc_instant(self.clock())
        return body['result']


class LiveEngine:
    """Single process, one unsettled order at a time; conservative reservations.

    Crash between reservation and intent creation leaves a reservation orphan:
    recovery halts; it never infers that an order is safe to resubmit.
    """
    def __init__(self, database, transport, *, clock=lambda: pd.Timestamp.now(tz='UTC'), enabled=False):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.lock = ProcessLock(database)
        self.gateway = RiskGateway(database)
        self.db = self.gateway.connection
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS live_orders (id TEXT PRIMARY KEY, client TEXT UNIQUE NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, txid TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS live_fills (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS live_values (name TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS live_audit (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, order_id TEXT)')
        self.transport, self.clock, self.enabled = transport, clock, enabled
        self._verified_candidate = None
        prior = self.value('fingerprint')
        if prior and prior != transport.fingerprint:
            self.close()
            raise PermissionError('Database belongs to another trading key')
        self.set_value('fingerprint', transport.fingerprint)
        # A crash after intent persistence is always UNKNOWN, never NEW again.
        self.db.execute("UPDATE live_orders SET state='UNKNOWN' WHERE state='SUBMITTING'")
        orphan = self.db.execute("SELECT order_id FROM reservations WHERE status='reserved' AND order_id NOT IN (SELECT id FROM live_orders)").fetchall()
        if orphan:
            self.gateway.kill()
            self.event('orphan_reservation')

    def close(self):
        self.gateway.close()
        self.lock.close()

    def value(self, name):
        row = self.db.execute('SELECT body FROM live_values WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_value(self, name, body):
        self.db.execute('INSERT OR REPLACE INTO live_values VALUES (?,?)', (name, canonical_json(body).decode()))

    def event(self, kind, order_id=None):
        self.db.execute('INSERT INTO live_audit(timestamp,kind,order_id) VALUES (?,?,?)',
                        (utc_instant(self.clock()).isoformat(), kind, order_id))

    def _write_allowed(self):
        if not self.enabled:
            raise PermissionError('Live engine disabled; explicit enablement required')
        if not self.transport.simulated:
            approval_check(self.transport.approval or {}, self.transport.evidence or {},
                           fingerprint=self.transport.fingerprint, now=utc_instant(self.clock()))
        check_trading_permissions(self.transport.request('GetApiKeyInfo'), self.clock())

    def _pages(self, endpoint, key, start, end):
        rows, offset, expected = {}, 0, None
        while True:
            params = {'start': start, 'end': end, 'ofs': offset, 'trades': 'true'}
            if endpoint == 'TradesHistory':
                params['consolidate_taker'] = 'false'
            page = self.transport.request(endpoint, params)
            count = int(page['count'])
            if expected is None:
                expected = count
            if expected != count or count > 10000:
                raise PermissionError('Unstable/oversized exchange history')
            current = page[key]
            if set(rows) & set(current):
                raise PermissionError('Repeated history page')
            rows.update(current)
            offset += len(current)
            if offset == count:
                return rows
            if not current or offset > count:
                raise PermissionError('Incomplete exchange history')

    @staticmethod
    def _balances(body):
        result = {k: str(money(v)) for k, v in body.items() if money(v)}
        if any(money(v) < 0 for v in result.values()):
            raise PermissionError('Negative/margin balance unsupported')
        return result

    def reconcile(self):
        """Stable account snapshot + full order/trade history from origin.

        New deposits/withdrawals/external orders halt; no automatic rebasing.
        Unfound UNKNOWN stays reserved forever until independently resolved.
        """
        try:
            check_trading_permissions(self.transport.request('GetApiKeyInfo'), self.clock())
            now = utc_instant(self.clock())
            origin = self.value('origin')
            start = (origin or {'timestamp': now.isoformat()})['timestamp']
            balances = self._balances(self.transport.request('Balance'))
            opened = self.transport.request('OpenOrders', {'trades': 'true'})['open']
            closed = self._pages('ClosedOrders', 'closed', utc_instant(start).timestamp(), now.timestamp())
            trades = self._pages('TradesHistory', 'trades', utc_instant(start).timestamp(), now.timestamp())
            if (balances != self._balances(self.transport.request('Balance'))
                    or opened != self.transport.request('OpenOrders', {'trades': 'true'})['open']
                    or utc_instant(self.clock()) - now > pd.Timedelta(seconds=60)):
                raise PermissionError('Account changed during reconciliation; retry observation only')
            if origin is None:
                if opened or trades or closed:
                    raise PermissionError('Initialize dedicated account while no orders/trades are active')
                origin = {'timestamp': now.isoformat(), 'balances': balances}
            expected = {k: money(v) for k, v in origin['balances'].items()}
            orders = self.db.execute('SELECT id,client,body,state,txid FROM live_orders').fetchall()
            by_client = {r[1]: r for r in orders}
            exchange = {**closed, **opened}
            matched = {}
            for txid, order in exchange.items():
                row = by_client.get(order.get('cl_ord_id'))
                if row is None:
                    raise PermissionError('External/unrecognized exchange order')
                body = json.loads(row[2])
                descr = order['descr']
                if (descr['pair'] not in body.get('pair_aliases', [body['pair']]) or descr['type'] != body['side']
                        or money(order['vol']) != money(body['quantity'])
                        or money(descr['price']) != money(body['limit_price'])
                        or (row[4] and row[4] != txid) or row[0] in matched):
                    raise PermissionError('Exchange order identity/quantity/limit mismatch')
                matched[row[0]] = (txid, order, body)
            totals = {}
            for trade_id, trade in trades.items():
                txid = trade['ordertxid']
                match = next((v for v in matched.values() if v[0] == txid), None)
                if match is None:
                    raise PermissionError('Unrecognized exchange fill')
                body = match[2]
                q, price, cost, fee = (money(trade[k]) for k in ('vol', 'price', 'cost', 'fee'))
                if (q <= 0 or price <= 0 or cost <= 0 or fee < 0
                        or abs(cost - q * price) > Decimal(1).scaleb(cost.as_tuple().exponent)
                        or money(trade.get('margin', 0)) != 0
                        or trade['type'] != body['side'] or trade['pair'] not in body.get('pair_aliases', [body['pair']])
                        or not utc_instant(start).timestamp() <= float(trade['time']) <= now.timestamp()
                        or (body['side'] == 'buy' and price > money(body['limit_price']))
                        or (body['side'] == 'sell' and price < money(body['limit_price']))
                        or fee > cost * money(body['fee_rate']) + Decimal('0.00000001')):
                    raise PermissionError('Exchange fill violates reserved terms')
                direction = 1 if body['side'] == 'buy' else -1
                expected[body['base_id']] = expected.get(body['base_id'], Decimal(0)) + direction * q
                expected[body['quote_id']] = expected.get(body['quote_id'], Decimal(0)) - direction * cost - fee
                totals[txid] = totals.get(txid, Decimal(0)) + q
                old = self.db.execute('SELECT body FROM live_fills WHERE id=?', (trade_id,)).fetchone()
                if old and json.loads(old[0]) != trade:
                    raise PermissionError('Exchange fill ID changed')
            if {k: str(v) for k, v in expected.items() if v} != balances:
                # Decimal scale differences are semantically equivalent.
                if {k: money(v) for k,v in balances.items()} != {k:v for k,v in expected.items() if v}:
                    raise PermissionError('Order/trade/balance reconciliation discrepancy')
            if not set(r[0] for r in self.db.execute('SELECT id FROM live_fills')) <= set(trades):
                raise PermissionError('Historical fills disappeared')
            for row in orders:
                if row[4] and row[0] not in matched and row[3] != 'rejected':
                    raise PermissionError('Known exchange order disappeared')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.set_value('origin', origin)
                self.set_value('snapshot', {'timestamp': now.isoformat(), 'balances': balances})
                for trade_id, trade in trades.items():
                    self.db.execute('INSERT OR IGNORE INTO live_fills VALUES (?,?)', (trade_id, canonical_json(trade).decode()))
                for order_id, (txid, order, body) in matched.items():
                    qty = totals.get(txid, Decimal(0))
                    if qty != money(order.get('vol_exec', 0)) or qty > money(body['quantity']):
                        raise PermissionError('Order and fill quantities disagree')
                    state = order['status']
                    if state not in TERMINAL | {'open', 'pending'}:
                        raise PermissionError('Unknown exchange order state')
                    if state == 'closed' and qty != money(body['quantity']):
                        raise PermissionError('Closed order not fully filled')
                    self.db.execute('UPDATE live_orders SET state=?,txid=? WHERE id=?', (state, txid, order_id))
                    if state in TERMINAL:
                        self.gateway.release_after_reconciliation(order_id)
                for order_id, in self.db.execute("SELECT id FROM live_orders WHERE state='rejected'").fetchall():
                    self.gateway.release_after_reconciliation(order_id)
                self.event('reconciled')
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            return self.inspect()
        except Exception:
            self.gateway.kill()
            self.event('reconciliation_failed')
            raise

    def _not_killed(self):
        if self.db.execute("SELECT value FROM controls WHERE name='kill'").fetchone()[0] == '1':
            raise NotSubmitted('Persistent kill switch is active')

    def submit(self, intent, *, account, quotes, rules, pair, base_id, quote_id):
        self._write_allowed()
        frozen = None
        if not self.transport.simulated:
            if self._verified_candidate != self.transport.evidence['candidate_sha256']:
                raise PermissionError('Real orders must originate from the verified frozen strategy interface')
            from .candidate import read_candidate
            root = Path(self.transport.evidence['artifact_directory'])
            frozen = read_candidate(root / self.transport.evidence['candidate_file'])['specification']
            if intent.instrument.symbol not in frozen['universe'] or not intent.decision_key.startswith(self._verified_candidate + ':'):
                raise PermissionError('Intent does not belong to the approved frozen candidate')
        metadata = None
        if not self.transport.simulated:
            metadata = self.transport.market(intent.instrument)
            if (pair, base_id, quote_id) != (metadata['pair'], metadata['base_id'], metadata['quote_id']):
                raise PermissionError('Order market identity differs from public Kraken metadata')
            rules = metadata['rules']
        self.reconcile()
        if self.db.execute("SELECT 1 FROM live_orders WHERE state NOT IN ('closed','canceled','expired','rejected')").fetchone():
            raise PermissionError('Unsettled/UNKNOWN order blocks new submission')
        snapshot = self.value('snapshot')
        balances = {k: money(v) for k, v in snapshot['balances'].items()}
        mapping = self.value('asset_map') or {}
        if not self.transport.simulated:
            for held in account.positions:
                held_market = self.transport.market(held)
                mapping[held.base] = held_market['base_id']
        mapping[intent.instrument.base] = base_id
        mapping['USD'] = quote_id
        expected = {mapping.get(k.base): v for k,v in account.positions.items() if v}
        expected[quote_id] = account.cash
        if {k:v for k,v in expected.items() if v} != {k:v for k,v in balances.items() if v}:
            raise PermissionError('Risk account snapshot differs from reconciled exchange balances')
        if not pd.Timedelta(0) <= utc_instant(self.clock()) - account.timestamp <= pd.Timedelta(seconds=60):
            raise PermissionError('Risk snapshot is stale')
        account = replace(account, timestamp=utc_instant(snapshot['timestamp']))
        # The same durable daily baseline must be reused for every decision.
        day = account.day_start.isoformat()
        baseline = self.value('daily_baseline')
        if baseline and baseline['day'] == day and money(baseline['equity']) != account.day_start_equity:
            raise PermissionError('Daily loss baseline changed')
        if baseline and baseline['day'] != day:
            raise PermissionError('New UTC day requires independently recorded baseline; use set_day_baseline')
        fee = money(self.transport.request('TradeVolume', {'pair': pair})['fees'][pair]['fee']) / 100
        if intent.fee_rate < fee:
            raise PermissionError('Reserved fee below account taker fee')
        if callable(quotes):
            quotes = quotes()
        if frozen:
            limits = frozen['risk_limits']
            equity = account.cash + sum((q * quotes[k].bid for k,q in account.positions.items()), money(0))
            exposure = sum((q * quotes[k].ask for k,q in account.positions.items()), money(0))
            added = intent.quantity * intent.limit_price if intent.side == 'buy' else money(0)
            asset = account.positions.get(intent.instrument, money(0)) * quotes[intent.instrument].ask + added
            loss = account.day_start_equity - equity + intent.quantity * max(intent.limit_price - quotes[intent.instrument].bid, money(0)) + intent.quantity * intent.limit_price * intent.fee_rate
            if (exposure + added > equity * money(limits.get('max_gross', 1))
                    or asset > equity * money(limits.get('max_position_weight', 1))
                    or loss >= account.day_start_equity * money(limits.get('max_daily_loss', 1))):
                raise PermissionError('Frozen candidate risk limits would be breached')
        approved = self.gateway.reserve(intent, account=account, quotes=quotes, rules=rules, now=self.clock())
        body = asdict(approved)
        body['instrument'] = asdict(approved.instrument)
        for k in ('quantity', 'limit_price', 'fee_rate', 'leverage'):
            body[k] = str(body[k])
        body.update(pair=pair, base_id=base_id, quote_id=quote_id,
                    pair_aliases=metadata['aliases'] if metadata else [pair])
        client = str(uuid.uuid5(uuid.NAMESPACE_URL, self.transport.fingerprint + ':' + intent.order_id))
        self.db.execute('INSERT INTO live_orders VALUES (?,?,?, ?,NULL)',
                        (intent.order_id, client, canonical_json(body).decode(), 'SUBMITTING'))
        self.set_value('asset_map', mapping)
        self.set_value('daily_baseline', {'day': day, 'equity': str(account.day_start_equity)})
        self.event('intent_durable', intent.order_id)
        def final_guard():
            self._not_killed()
            now = utc_instant(self.clock())
            if not pd.Timedelta(0) <= now - account.timestamp <= pd.Timedelta(seconds=60):
                raise NotSubmitted('Account became stale before wire submission')
            if not pd.Timedelta(0) <= now - rules.timestamp <= pd.Timedelta(hours=24):
                raise NotSubmitted('Rules became stale before wire submission')
            if any(not pd.Timedelta(0) <= now - q.timestamp <= pd.Timedelta(seconds=5) for q in quotes.values()):
                raise NotSubmitted('Quote became stale before wire submission')
        submitting = False
        try:
            self.transport.request('CancelAllOrdersAfter', {'timeout': 60}, pre_send=self._not_killed)
            submitting = True
            response = self.transport.request('AddOrder', {'pair': pair, 'type': approved.side,
                'ordertype': 'limit', 'volume': str(approved.quantity), 'price': str(approved.limit_price),
                'cl_ord_id': client, 'oflags': 'fciq', 'timeinforce': 'IOC', 'stptype': 'cancel-newest',
                'deadline': (utc_instant(self.clock()) + pd.Timedelta(seconds=30)).isoformat()}, pre_send=final_guard)
            txids = response.get('txid')
            if not isinstance(txids, list) or len(txids) != 1 or not isinstance(txids[0], str):
                raise AmbiguousSubmission('Missing unique order acknowledgement')
            self.db.execute("UPDATE live_orders SET state='pending',txid=? WHERE id=?", (txids[0], intent.order_id))
            self.event('acknowledged', intent.order_id)
        except NotSubmitted:
            self.db.execute("UPDATE live_orders SET state='rejected' WHERE id=?", (intent.order_id,))
            self.gateway.kill()
            self.event('wire_send_prevented', intent.order_id)
            self.reconcile()
            raise
        except ExchangeRejected:
            state = 'rejected' if submitting else 'UNKNOWN'
            self.db.execute('UPDATE live_orders SET state=? WHERE id=?', (state, intent.order_id))
            self.event('submission_' + state.lower(), intent.order_id)
            self.gateway.kill()
            if submitting:
                self.reconcile()
            raise
        except BaseException:
            self.db.execute("UPDATE live_orders SET state='UNKNOWN' WHERE id=?", (intent.order_id,))
            self.gateway.kill()
            self.event('submission_unknown', intent.order_id)
            raise
        return self.inspect()

    def execute_strategy(self, runner, *, historical_data, account, quotes, rules, fee_rates, metadata):
        from .live_strategy import FrozenLiveStrategy
        if not isinstance(runner, FrozenLiveStrategy):
            raise TypeError('Verified FrozenLiveStrategy required')
        if not self.transport.simulated and runner.candidate_sha256 != self.transport.evidence['candidate_sha256']:
            raise PermissionError('Strategy differs from approved candidate')
        if self.transport.simulated:
            self.set_value('asset_map',{k.base:v['base_id'] for k,v in metadata.items()})
        current_quotes = quotes() if callable(quotes) else quotes
        intents = runner.plan(historical_data=historical_data, account=account, quotes=current_quotes,
                              rules=rules, timestamp=self.clock(), fee_rates=fee_rates)
        if not intents:
            return {'decision':'hold', 'candidate_sha256':runner.candidate_sha256, 'orders_submitted':0, 'remaining_rebalances':0}
        # One unsettled lifecycle at a time; re-run after reconciliation for the
        # next asset. IDs retain the daily decision identity across restarts.
        pending = [i for i in intents if not self.db.execute(
            'SELECT 1 FROM reservations WHERE decision_key=?',(i.decision_key,)).fetchone()]
        intent = pending[0] if pending else None
        if intent is None:
            return {'decision':'already_processed', 'candidate_sha256':runner.candidate_sha256, 'orders_submitted':0, 'remaining_rebalances':0, 'residual_adjustments':len(intents)}
        meta=metadata[intent.instrument]
        self._verified_candidate=runner.candidate_sha256
        try:
            result = self.submit(intent,account=account,quotes=quotes,rules=rules[intent.instrument],
                pair=meta['pair'],base_id=meta['base_id'],quote_id=meta['quote_id'])
            result.update(decision='submitted', orders_submitted=1, remaining_rebalances=len(pending)-1)
            return result
        finally:
            self._verified_candidate=None

    def account_from_snapshot(self, metadata, day_start_equity):
        snapshot=self.value('snapshot')
        if snapshot is None:
            raise PermissionError('Reconcile before constructing risk account')
        balances={k:money(v) for k,v in snapshot['balances'].items()}
        quote_ids={v['quote_id'] for v in metadata.values()}
        if len(quote_ids)!=1:
            raise PermissionError('Ambiguous quote asset identity')
        quote_id=next(iter(quote_ids))
        allowed={v['base_id'] for v in metadata.values()}|{quote_id}
        if set(balances)-allowed:
            raise PermissionError('Unsupported account assets present')
        return AccountSnapshot(snapshot['timestamp'],utc_instant(self.clock()).normalize(),
            balances.get(quote_id,money(0)),
            {k:balances[v['base_id']] for k,v in metadata.items() if balances.get(v['base_id'],0)},
            day_start_equity)

    def rebalance_strategy(self, runner, *, historical_data, quotes, rules, fee_rates,
                           metadata, day_start_equity, max_orders=1):
        if not 1 <= max_orders <= len(metadata):
            raise ValueError('max_orders must be 1..frozen universe size')
        decisions=[]
        submitted=0
        for _ in range(max_orders):
            self.reconcile()
            if self.db.execute("SELECT 1 FROM live_orders WHERE state NOT IN ('closed','canceled','expired','rejected')").fetchone():
                return dict(self.inspect(),decisions=decisions,orders_submitted=submitted,
                            remaining_rebalances='blocked_by_unsettled_order')
            account=self.account_from_snapshot(metadata,day_start_equity)
            result=self.execute_strategy(runner,historical_data=historical_data,account=account,
                quotes=quotes,rules=rules,fee_rates=fee_rates,metadata=metadata)
            decisions.append(result['decision'])
            submitted+=result.get('orders_submitted',0)
            if result['decision']!='submitted':
                break
        self.reconcile()
        if self.db.execute("SELECT 1 FROM live_orders WHERE state NOT IN ('closed','canceled','expired','rejected')").fetchone():
            remaining='blocked_by_unsettled_order'
            residual=None
        else:
            fresh=quotes() if callable(quotes) else quotes
            planned=runner.plan(historical_data=historical_data,account=self.account_from_snapshot(metadata,day_start_equity),
                quotes=fresh,rules=rules,timestamp=self.clock(),fee_rates=fee_rates)
            remaining=sum(not self.db.execute('SELECT 1 FROM reservations WHERE decision_key=?',(i.decision_key,)).fetchone() for i in planned)
            residual=len(planned)
        return dict(self.inspect(),decisions=decisions,orders_submitted=submitted,
                    remaining_rebalances=remaining,residual_adjustments=residual)

    def set_day_baseline(self, equity, *, observed_at):
        """Use a genuinely recorded midnight mark; never infer a reset on restart."""
        observed_at = utc_instant(observed_at)
        now = utc_instant(self.clock())
        if observed_at != now.normalize() or money(equity) <= 0:
            raise PermissionError('Recorded UTC-midnight equity required')
        if self.value('daily_baseline') and self.value('daily_baseline')['day'] == observed_at.isoformat():
            raise PermissionError('Current daily baseline already frozen')
        self.set_value('daily_baseline', {'day': observed_at.isoformat(), 'equity': str(money(equity))})
        self.event('day_baseline_recorded')

    def refresh_deadman(self):
        self._write_allowed()
        return self.transport.request('CancelAllOrdersAfter', {'timeout': 60}, pre_send=self._not_killed)

    def cancel(self, order_id):
        self._write_allowed()
        row = self.db.execute('SELECT client,state FROM live_orders WHERE id=?', (order_id,)).fetchone()
        if row is None or row[1] in TERMINAL:
            raise ValueError('Unsettled local order required')
        self.transport.request('CancelOrder', {'cl_ord_id': row[0]})
        self.event('cancel_requested', order_id)
        # Cancel acknowledgement does not release reservation or imply no fill.
        return self.reconcile()

    def kill(self):
        self.gateway.kill()
        self.event('operator_kill')
        # Local kill is guaranteed even when exchange is unavailable.
        return self.inspect()

    def resume(self):
        self.reconcile()
        if self.db.execute("SELECT 1 FROM live_orders WHERE state='UNKNOWN'").fetchone():
            raise PermissionError('UNKNOWN order prevents resume')
        if self.db.execute("SELECT 1 FROM reservations WHERE status='reserved' AND order_id NOT IN (SELECT id FROM live_orders)").fetchone():
            raise PermissionError('Orphan reservation requires independent exchange investigation')
        self.gateway.clear_kill()
        self.event('operator_resume')

    def inspect(self):
        return {'halted': self.db.execute("SELECT value FROM controls WHERE name='kill'").fetchone()[0] == '1',
                'orders': [{'order_id': r[0], 'client_id': r[1], 'state': r[2], 'exchange_id': r[3]}
                           for r in self.db.execute('SELECT id,client,state,txid FROM live_orders ORDER BY rowid')],
                'fills': self.db.execute('SELECT COUNT(*) FROM live_fills').fetchone()[0],
                'snapshot': self.value('snapshot'), 'daily_baseline': self.value('daily_baseline')}
