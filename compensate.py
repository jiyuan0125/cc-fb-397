# -*- coding: utf-8 -*-
"""库存日结补偿：迟到单据补入已结区间，从单据日起连锁重算到结过的最后一天。

源：movements.csv(原始流水) + late_arrivals.csv(迟到单据)
输出：
  new_balance.csv          新日结表（商品,仓,日期,日结量）
  compensation_register.csv 补偿登记（每张单据影响到的每一天一行）
  skylight_register.csv    开天窗登记（单据日早于日结第一天，补不了）
  summary.txt              汇总报告（行数与新日结表、登记对账）
"""
import csv
import sys
from collections import defaultdict
from datetime import date, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else '.'
OUT = sys.argv[2] if len(sys.argv) > 2 else '.'

def path(name, base):
    return base.rstrip('/') + '/' + name

def sign(t):
    if t == '入库':
        return 1
    if t == '出库':
        return -1
    raise ValueError('未知类型: %r' % t)

def daterange(d1, d2):
    d = d1
    while d <= d2:
        yield d
        d += timedelta(days=1)

# ---- 读原日结表 ----
old_bal = {}  # (商品,仓,日期str) -> 日结量
with open(path('balance.csv', BASE), newline='', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        old_bal[(r['商品'], r['仓'], r['日期'])] = int(r['日结量'])

combos = sorted({(g, w) for g, w, _ in old_bal})
dates = sorted({d for _, _, d in old_bal})
first_day, last_day = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
all_days = list(daterange(first_day, last_day))
day_strs = [d.isoformat() for d in all_days]
assert day_strs == dates, '日结表日期不连续'

# ---- 读原始流水，按天汇总变动 ----
mov_delta = defaultdict(int)  # (商品,仓,日期str) -> 净变动
with open(path('movements.csv', BASE), newline='', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        d = r['时间'][:10]
        mov_delta[(r['商品'], r['仓'], d)] += sign(r['类型']) * int(r['数量'])

# ---- 读迟到单据：同一单据号只认头一回；早于日结第一天的进开天窗 ----
late_docs = []      # 可补偿单据
skylight = []       # 开天窗单据
dup_docs = []       # 被丢弃的重复单据
seen = set()
with open(path('late_arrivals.csv', BASE), newline='', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        doc = r['单据号']
        if doc in seen:
            dup_docs.append(r)
            continue
        seen.add(doc)
        if date.fromisoformat(r['单据日']) < first_day:
            skylight.append(r)
        else:
            late_docs.append(r)

late_delta = defaultdict(int)  # (商品,仓,日期str) -> 净变动
for r in late_docs:
    late_delta[(r['商品'], r['仓'], r['单据日'])] += sign(r['类型']) * int(r['数量'])

# ---- 重算：期初 = 原第一天日结 - 当日流水变动；之后逐日累加 流水+迟到 ----
new_bal = {}
for g, w in combos:
    opening = old_bal[(g, w, day_strs[0])] - mov_delta.get((g, w, day_strs[0]), 0)
    run = opening
    for ds in day_strs:
        run += mov_delta.get((g, w, ds), 0) + late_delta.get((g, w, ds), 0)
        new_bal[(g, w, ds)] = run

# 校验：不加迟到单据重算必须还原原日结表
for g, w in combos:
    opening = old_bal[(g, w, day_strs[0])] - mov_delta.get((g, w, day_strs[0]), 0)
    run = opening
    for ds in day_strs:
        run += mov_delta.get((g, w, ds), 0)
        assert run == old_bal[(g, w, ds)], '流水与原日结不符: %s %s %s' % (g, w, ds)

# ---- 写新日结表 ----
changed_rows = 0
with open(path('new_balance.csv', OUT), 'w', newline='', encoding='utf-8') as f:
    wtr = csv.writer(f)
    wtr.writerow(['商品', '仓', '日期', '日结量'])
    for g, w in combos:
        for ds in day_strs:
            if new_bal[(g, w, ds)] != old_bal[(g, w, ds)]:
                changed_rows += 1
            wtr.writerow([g, w, ds, new_bal[(g, w, ds)]])

# ---- 写补偿登记：每张单据影响的每一天一行 ----
reg_rows = 0
with open(path('compensation_register.csv', OUT), 'w', newline='', encoding='utf-8') as f:
    wtr = csv.writer(f)
    wtr.writerow(['单据号', '商品', '仓', '单据日', '类型', '数量',
                  '影响日期', '原日结量', '新日结量'])
    for r in sorted(late_docs, key=lambda x: (x['单据日'], x['单据号'])):
        g, w = r['商品'], r['仓']
        for d in daterange(date.fromisoformat(r['单据日']), last_day):
            ds = d.isoformat()
            wtr.writerow([r['单据号'], g, w, r['单据日'], r['类型'], r['数量'],
                          ds, old_bal[(g, w, ds)], new_bal[(g, w, ds)]])
            reg_rows += 1

# ---- 写开天窗登记 ----
with open(path('skylight_register.csv', OUT), 'w', newline='', encoding='utf-8') as f:
    wtr = csv.writer(f)
    wtr.writerow(['单据号', '商品', '仓', '单据日', '类型', '数量', '到达时间', '原因'])
    for r in skylight:
        wtr.writerow([r['单据号'], r['商品'], r['仓'], r['单据日'], r['类型'],
                      r['数量'], r['到达时间'],
                      '单据日早于日结第一天%s，无法补偿' % day_strs[0]])

# ---- 汇总报告：三处行数对账 ----
new_bal_rows = len(combos) * len(day_strs)
# 登记按 单据×日期 记行；同一(商品,仓,日期)可能被多张单据影响，去重后才是受影响的日结行数
affected_cells = {(r['商品'], r['仓'], ds)
                  for r in late_docs
                  for ds in [d.isoformat() for d in daterange(date.fromisoformat(r['单据日']), last_day)]}
lines = [
    '库存日结补偿汇总报告',
    '日结区间: %s ~ %s（%d天，%d个商品仓组合）' % (day_strs[0], day_strs[-1], len(day_strs), len(combos)),
    '',
    '迟到单据: 共收到%d行' % (len(late_docs) + len(skylight) + len(dup_docs)),
    '  补偿单据: %d张' % len(late_docs),
    '  开天窗单据: %d张（单据日早于%s，见 skylight_register.csv）' % (len(skylight), day_strs[0]),
    '  重复丢弃: %d行（同一单据号只认头一回: %s）'
    % (len(dup_docs), ', '.join(sorted({r['单据号'] for r in dup_docs})) or '无'),
    '',
    '行数对账:',
    '  新日结表 new_balance.csv: %d行（= %d组合 × %d天，与原日结表行数一致）'
    % (new_bal_rows, len(combos), len(day_strs)),
    '  补偿登记 compensation_register.csv: %d行（按 单据×影响日期 计）' % reg_rows,
    '  登记去重到(商品,仓,日期)的受影响日结行: %d行' % len(affected_cells),
    '  新日结表中实际改动的行: %d行' % changed_rows,
    '  对账结果: %s' % ('一致（登记去重受影响行数 = 新日结表改动行数 = %d）' % changed_rows
                        if len(affected_cells) == changed_rows else '不一致，需排查！'),
]
report = '\n'.join(lines) + '\n'
with open(path('summary.txt', OUT), 'w', encoding='utf-8') as f:
    f.write(report)
sys.stdout.write(report)
