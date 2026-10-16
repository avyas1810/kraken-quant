"""Deterministic Kraken-style exchange; never connects to a real endpoint."""
from decimal import Decimal
import uuid
import pandas as pd
from .live import AmbiguousSubmission, ExchangeRejected, PERMISSIONS
from .risk import money


class SimulatedKraken:
    simulated = True
    fingerprint = 'simulation-only'
    approval = evidence = None

    def __init__(self, *, clock=lambda: pd.Timestamp.now(tz='UTC'), cash='1000'):
        self.clock = clock
        self.balances = {'ZUSD': money(cash)}
        self.orders, self.trades, self.calls = {}, {}, []
        self.mode = 'success'
        self.deadline = None
        self.permissions = sorted(PERMISSIONS)

    def request(self, endpoint, params=None, *, pre_send=None):
        if pre_send is not None:
            pre_send()
        params = params or {}
        self.calls.append((endpoint, dict(params)))
        if endpoint == 'GetApiKeyInfo':
            return {'permissions': self.permissions, 'validUntil': 0, 'queryFrom': 0, 'queryTo': 0}
        if endpoint == 'Balance':
            return {k: str(v) for k,v in self.balances.items() if v}
        if endpoint == 'TradeVolume':
            return {'fees': {params['pair']: {'fee': '0.4'}}}
        if endpoint in {'OpenOrders', 'ClosedOrders'}:
            opened = endpoint == 'OpenOrders'
            rows = {k:v.copy() for k,v in self.orders.items() if (v['status'] in {'open','pending'}) == opened}
            if opened:
                return {'open': rows}
            start, end = float(params.get('start', 0)), float(params.get('end', 1e20))
            rows = {k:v for k,v in rows.items() if start <= v['closetm'] <= end}
            return {'closed': dict(list(rows.items())[int(params.get('ofs',0)):][:50]), 'count':len(rows)}
        if endpoint == 'TradesHistory':
            start, end = float(params.get('start', 0)), float(params.get('end', 1e20))
            rows = {k:v.copy() for k,v in self.trades.items() if start <= v['time'] <= end}
            return {'trades':dict(list(rows.items())[int(params.get('ofs',0)):][:50]), 'count':len(rows)}
        if endpoint == 'CancelAllOrdersAfter':
            self.deadline = self.clock() + pd.Timedelta(seconds=int(params['timeout']))
            return {'currentTime': self.clock().isoformat(), 'triggerTime': self.deadline.isoformat()}
        if endpoint == 'CancelOrder':
            found = [k for k,v in self.orders.items() if v['cl_ord_id'] == params['cl_ord_id'] and v['status'] in {'open','pending'}]
            if not found:
                raise ExchangeRejected('No simulated order')
            for key in found:
                self.orders[key]['status'] = 'canceled'
                self.orders[key]['closetm'] = self.clock().timestamp()
            return {'count':len(found)}
        if endpoint == 'AddOrder':
            if self.mode == 'reject':
                raise ExchangeRejected('Simulated explicit rejection')
            if self.mode == 'timeout_before_acceptance':
                raise AmbiguousSubmission('Simulated network timeout')
            if any(v['cl_ord_id'] == params['cl_ord_id'] for v in self.orders.values()):
                raise ExchangeRejected('Duplicate client order ID')
            quantity, price = money(params['volume']), money(params['price'])
            if params['type'] == 'buy' and quantity * price * Decimal('1.004') > self.balances.get('ZUSD',0):
                raise ExchangeRejected('Simulated insufficient funds')
            txid = 'SIM-' + uuid.uuid4().hex
            self.orders[txid] = {'cl_ord_id': params['cl_ord_id'], 'descr':{'pair':params['pair'], 'type':params['type'], 'price':str(price)},
                'vol':str(quantity), 'vol_exec':'0', 'status':'open', 'closetm':0}
            if self.mode not in {'resting','partial_open'}:
                self.fill(txid, quantity / 2 if self.mode == 'partial' else quantity)
                self.orders[txid]['status'] = 'canceled' if self.mode == 'partial' else 'closed'
                self.orders[txid]['closetm'] = self.clock().timestamp()
            elif self.mode == 'partial_open':
                self.fill(txid, quantity / 2)
            if self.mode == 'lost_ack':
                raise AmbiguousSubmission('Simulated acknowledgement lost after fill')
            return {'txid':[txid]}
        raise PermissionError('Unsupported simulated endpoint')

    def fill(self, txid, quantity):
        row = self.orders[txid]
        quantity, price = money(quantity), money(row['descr']['price'])
        if quantity <= 0 or money(row['vol_exec']) + quantity > money(row['vol']):
            raise ValueError('Invalid simulated partial fill')
        cost, fee = quantity * price, quantity * price * Decimal('.004')
        base = 'XXBT' if row['descr']['pair'] == 'XXBTZUSD' else row['descr']['pair'].removesuffix('ZUSD')
        direction = 1 if row['descr']['type'] == 'buy' else -1
        if direction < 0 and quantity > self.balances.get(base,0):
            raise ExchangeRejected('Simulated short selling prohibited')
        self.balances[base] = self.balances.get(base,Decimal(0)) + direction * quantity
        self.balances['ZUSD'] -= direction * cost + fee
        row['vol_exec'] = str(money(row['vol_exec']) + quantity)
        trade = {'ordertxid':txid, 'pair':row['descr']['pair'], 'type':row['descr']['type'], 'vol':str(quantity),
                 'price':str(price), 'cost':str(cost), 'fee':str(fee), 'time':self.clock().timestamp()}
        self.trades['TRADE-' + uuid.uuid4().hex] = trade
        if money(row['vol_exec']) == money(row['vol']):
            row['status'], row['closetm'] = 'closed', self.clock().timestamp()

    def expire_deadman(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            for row in self.orders.values():
                if row['status'] in {'open','pending'}:
                    row['status'], row['closetm'] = 'canceled', self.clock().timestamp()
