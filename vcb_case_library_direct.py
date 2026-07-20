# -*- coding: utf-8 -*-
"""
VCB/1757 工单案例库一体化流程（最终版）

默认直接运行：
    python vcb_case_library_direct.py
等价于：
    python vcb_case_library_direct.py extract-build \
        --work-order-xlsx 工单.xlsx \
        --data-dir 01_数据整理 \
        --save-dir case_library

核心逻辑：
1) 从原始工单 xlsx 提取 case_input_all_times.csv；
2) 读取 case_input_all_times.csv，按 train + fault_time 查 01_数据整理；
3) 局部生成 events，2 秒切 segment，筛选包含 VCB target_column 的 segment；
4) 生成 node_counts、node_index、vectors，保存 case_library；
5) 缺少日期 CSV 时不再扫描全部 CSV，而是直接记录 no_date_csv；
6) 464 变量按旧版 process464 逻辑处理，即按 cur - prev 的变化幅度归并；
7) 支持 update-from-xlsx 增量更新：先读 process_log.csv 判断已有工单号，只解析新增工单。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd
from tqdm import tqdm
import shutil
import subprocess


# ============================================================
# 0. 全局配置
# ============================================================

DEFAULT_WORK_ORDER_XLS = '工单.xls'
DEFAULT_WORK_ORDER_XLSX = '工单.xlsx'
DEFAULT_DATA_DIR = '01_数据整理'
DEFAULT_SAVE_DIR = 'case_library'
DEFAULT_FULL_CASE_CSV = 'case_input_all_times.csv'
DEFAULT_UPDATE_CASE_CSV = 'update_case_input_all_times.csv'
DEFAULT_NO_TIME_CSV = 'case_input_no_time.csv'
DEFAULT_UPDATE_NO_TIME_CSV = 'update_case_input_no_time.csv'
DEFAULT_MISSING_DATE_CSV = 'missing_date_csv.csv'
DEFAULT_GATEWAY_WORK_ORDER_ROOT = 'gateway_downloads/work_orders'

# Qdrant 向量库连接
DEFAULT_QDRANT_URL = os.getenv('QDRANT_URL', 'http://10.0.1.87:6333')
DEFAULT_QDRANT_COLLECTION = 'vcb_case_library'

# 灾备：把 case_library/ 全部文件 + 全量故障时间 CSV 打成带时间戳的 zip
DEFAULT_BACKUP_DIR = 'backups'
DEFAULT_BACKUP_KEEP_LAST = 10
BACKUP_PREFIX = 'case_library_backup_'

# 原始数据是 UTC，工单时间一般是北京时间 CST
CST_TO_UTC_HOURS = 8

# 读取窗口：为了知道故障前状态，读取时间比分析时间更长
READ_BEFORE_MIN = 30
READ_AFTER_MIN = 30

# 真正参与 segment 分析的窗口
ANALYZE_BEFORE_MIN = 30
ANALYZE_AFTER_MIN = 5

# 2 秒切 segment
SEG_GAP_SECONDS = 2

# Main fault segment selection thresholds.
MAIN_SEGMENT_BEFORE_SECONDS = 120
MAIN_SEGMENT_AFTER_FALLBACK_SECONDS = 30
MAIN_SEGMENT_MAX_ABS_SECONDS = 180
# If there is no VCB 1->0 transition in the 10 minutes before the fault,
# allow the nearest post-fault transition within 5 minutes.
MAIN_SEGMENT_BEFORE_GUARD_SECONDS = 10 * 60
MAIN_SEGMENT_AFTER_EXTENDED_FALLBACK_SECONDS = 5 * 60

# distance / 145 附加特征最近点容忍时间
NEAREST_FEATURE_TOLERANCE_SECONDS = 10

# distance 分箱
DISTANCE_BIN_SIZE = 5


# ============================================================
# 1. target column
# ============================================================

target_column = """1157_3_1_01_sdr
1172_3_1_01_sdr
1194_3_1_01_sdr
904_2_1_02_sd
904_2_1_05_sd
904_2_1_09_sd
936_2_1_02_sd
936_2_1_05_sd
936_2_1_09_sd""".split('\n')

target_column_comb = ['dir_' + i for i in target_column]

ramping_finish_column = """943_5_1_02_sd
911_5_1_02_sd
943_5_1_05_sd
911_5_1_05_sd
943_5_1_09_sd
911_5_1_09_sd""".split('\n')

ramping_finish_column_comb = ['dir_' + i for i in ramping_finish_column]

vcb_to_ramping_finish = {
    '904_2_1_02_sd': '911_5_1_02_sd',
    '904_2_1_05_sd': '911_5_1_05_sd',
    '904_2_1_09_sd': '911_5_1_09_sd',
    '936_2_1_02_sd': '943_5_1_02_sd',
    '936_2_1_05_sd': '943_5_1_05_sd',
    '936_2_1_09_sd': '943_5_1_09_sd',
}

vcb_to_ramping_finish_dim = {
    'dir_' + vcb_code: 'dir_' + ramping_code
    for vcb_code, ramping_code in vcb_to_ramping_finish.items()
}

TARGET_CAR_VCB_NODE = {
    'dir_904_2_1': 'target_vcb_904',
    'dir_936_2_1': 'target_vcb_936',
}

TARGET_CAR_RAMPING_NODE = {
    'dir_911_5_1': 'target_ramping_finish_911',
    'dir_943_5_1': 'target_ramping_finish_943',
}


# ============================================================
# 2. 列名识别
# ============================================================

CASE_ID_ALIASES = [
    'work_order_no', 'Work Order NO.', 'Work Order No.', 'Work Order No',
    '工单号', 'WO', 'wo', 'work_order_id', 'WorkOrder', 'Work Order',
    'order_id', 'case_id'
]

TRAIN_ALIASES = [
    'train', 'Train', 'Trainset', 'trainset',
    '车号', '车组', 'equipInstance', 'train_id'
]

FAULT_TIME_ALIASES = [
    'fault_time', '故障时间', '发生时间', '报警时间',
    'alarm_time', 'time', 'Time', 'event_time'
]

CAR_NO_ALIASES = [
    'car_no', 'carno', 'CarNO', 'CarNO.', 'Car No', 'Car No.',
    'car', 'Car', '车厢', '车厢号', '车辆号'
]

DESC_TEXT_ALIASES = [
    'Long Description', 'LongDescription', 'long_description', 'long description',
    'Failure Long Description', 'Failure LongDescription',
    'Remark Description', 'Description', 'description',
    '故障描述', '处理描述', '维修描述'
]

OUTPUT_COLUMNS = ['work_order_no', 'train', 'car_no', 'fault_time', 'time_order', 'remark']


def pick_col(df: pd.DataFrame, aliases: Sequence[str], required: bool = True, name: str = 'column') -> Optional[str]:
    cols = list(df.columns)

    for a in aliases:
        if a in cols:
            return a

    lower_map = {str(c).strip().lower(): c for c in cols}
    for a in aliases:
        key = str(a).strip().lower()
        if key in lower_map:
            return lower_map[key]

    if required:
        raise ValueError(f'找不到{name}列。可用列为: {cols}，候选列名为: {list(aliases)}')
    return None


def convert_xls_to_xlsx_if_needed(input_path, output_path=None):
    """
    简单版 xls -> xlsx 转换。
    如果输入是 xlsx，直接返回。
    如果输入是 xls，则用 pandas 读取所有 sheet 后保存为 xlsx。
    """
    input_path = Path(input_path).expanduser().resolve()

    # 如果传入的是 xlsx，直接使用
    if str(input_path).lower().endswith('.xlsx'):
        if input_path.exists():
            return str(input_path)

        # 如果默认 工单.xlsx 不存在，但同名 工单.xls 存在，则自动转
        xls_candidate = input_path.with_suffix('.xls')
        if xls_candidate.exists():
            return convert_xls_to_xlsx_if_needed(xls_candidate, input_path)

        raise FileNotFoundError(f'找不到工单文件: {input_path}')

    # 如果不是 xls，报错
    if not str(input_path).lower().endswith('.xls'):
        raise ValueError(f'只支持 .xls 或 .xlsx 文件: {input_path}')

    if not input_path.exists():
        raise FileNotFoundError(f'找不到工单xls文件: {input_path}')

    # 输出路径
    if output_path is None:
        output_path = input_path.with_suffix('.xlsx')
    else:
        output_path = Path(output_path).expanduser().resolve()

    # 如果 xlsx 已经存在，仅当它不旧于 xls 时直接使用；
    # 用户覆盖工单.xls 后，应自动刷新同名 xlsx，避免继续读取旧文件。
    if output_path.exists():
        if output_path.stat().st_mtime >= input_path.stat().st_mtime:
            print(f'已存在最新 xlsx 文件，直接使用: {output_path}')
            return str(output_path)
        print(f'检测到 xls 比现有 xlsx 更新，将覆盖刷新: {output_path}')

    print('检测到 xls 文件，开始转换为 xlsx:')
    print('输入:', input_path)
    print('输出:', output_path)

    # 读取所有 sheet
    sheets = pd.read_excel(
        input_path,
        sheet_name=None,
        engine='xlrd',
        dtype=str
    )

    # 写入 xlsx
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        for sheet_name, df in sheets.items():
            # Excel sheet 名最长 31 个字符
            safe_sheet_name = str(sheet_name)[:31]
            df.to_excel(writer, sheet_name=safe_sheet_name, index=False)

    print(f'转换成功: {output_path}')
    return str(output_path)

# ============================================================
# 3. 基础工具
# ============================================================


def normalize_train(train: Any) -> str:
    s = '' if pd.isna(train) else str(train).strip()
    if s == '':
        return ''
    if s.startswith('T'):
        return s
    # 兼容 Excel 读成 25.0 的情况
    try:
        f = float(s)
        if f.is_integer():
            return 'T' + str(int(f))
    except Exception:
        pass
    return 'T' + s


def normalize_car_no(car_no: Any) -> str:
    s = '' if pd.isna(car_no) else str(car_no).strip()
    if s == '' or s.lower() in ['nan', 'none', 'null']:
        return ''
    digits = ''.join(ch for ch in s if ch.isdigit())
    if digits == '':
        return ''
    try:
        return f'{int(digits):02d}'
    except Exception:
        return digits[-2:].zfill(2)


def normalize_code(code: Any) -> str:
    code = str(code).strip()
    if code.startswith('dir_'):
        return code[4:]
    return code


def to_dim(code: Any) -> str:
    return 'dir_' + normalize_code(code)


def get_vcb_target_dims_for_car(car_no: Any = None) -> set[str]:
    car = normalize_car_no(car_no)
    if car in {'02', '05', '09'}:
        return {f'dir_904_2_1_{car}_sd', f'dir_936_2_1_{car}_sd'}
    return set(target_column_comb)


def get_ramping_finish_dims_for_car(car_no: Any = None) -> set[str]:
    car = normalize_car_no(car_no)
    if car in {'02', '05', '09'}:
        return {f'dir_911_5_1_{car}_sd', f'dir_943_5_1_{car}_sd'}
    return set(ramping_finish_column_comb)


def target_car_normalized_dim(dim: Any, car_no: Any = None) -> Optional[str]:
    """Normalize car-specific target channel names so cross-car cases align."""
    car = normalize_car_no(car_no)
    dim_text = str(dim)
    if car not in {'02', '05', '09'}:
        return dim_text

    for prefix, normalized in TARGET_CAR_VCB_NODE.items():
        if dim_text == f'{prefix}_{car}_sd':
            return normalized

    for prefix, normalized in TARGET_CAR_RAMPING_NODE.items():
        if dim_text == f'{prefix}_{car}_sd':
            return normalized

    if dim_text in set(target_column_comb) or dim_text in set(ramping_finish_column_comb):
        return None

    return dim_text


def parse_time_to_utc(time_value: Any, time_is_cst: bool = True) -> pd.Timestamp:
    t = pd.to_datetime(time_value)
    if time_is_cst:
        t = t - pd.Timedelta(hours=CST_TO_UTC_HOURS)
    return t


def is_distance_code(code: Any) -> bool:
    name = str(code).lower()
    return 'distance' in name or '距离' in str(code)


def is_145_code(code: Any) -> bool:
    return normalize_code(code) == '145_0_8_01_sdr'


def is_ramping_finish_code(code: Any) -> bool:
    return normalize_code(code) in set(ramping_finish_column)


def is_464_code(code: Any) -> bool:
    return '464' in str(code)


def safe_str(x: Any) -> str:
    if pd.isna(x):
        return ''
    return str(x).strip()


# ============================================================
# 4. 145 / 464 / distance 处理
# ============================================================


def bin_145_value(x: Any) -> str:
    x = float(x)
    if x == 0:
        return '0'
    elif 0 < x < 20:
        return '0-20'
    elif 20 <= x < 40:
        return '20-40'
    elif 40 <= x < 60:
        return '40-60'
    elif 60 <= x < 80:
        return '60-80'
    elif 80 <= x < 100:
        return '80-100'
    else:
        return '100+'


def bin_145_delta(delta: Any) -> str:
    try:
        delta = float(delta)
    except Exception:
        return 'stable'

    if delta > 0:
        return 'up'
    elif delta < 0:
        return 'down'
    else:
        return 'stable'


def process_464_event(
    train: str,
    dim: str,
    prev_time: Any,
    prev_value: Any,
    cur_time: Any,
    cur_value: Any,
) -> Optional[Tuple[Any, ...]]:

    diff = float(cur_value) - float(prev_value)

    if diff >= 15000:
        return (train, dim, prev_time, 0, cur_time, 30000)
    if diff <= -15000:
        return (train, dim, prev_time, 30000, cur_time, 0)
    if 200 <= diff < 15000:
        return (train, dim, prev_time, 0, cur_time, 15000)
    if -15000 < diff <= -200:
        return (train, dim, prev_time, 15000, cur_time, 0)
    return None


def discretize_distance(raw: Any, delta: Any) -> Tuple[int, int]:
    try:
        raw_bin = int(float(raw) // DISTANCE_BIN_SIZE)
    except Exception:
        raw_bin = -999999

    try:
        delta = float(delta)
    except Exception:
        delta = 0.0

    if delta > 0:
        delta_bin = 1
    elif delta < 0:
        delta_bin = -1
    else:
        delta_bin = 0

    return raw_bin, delta_bin


# ============================================================
# 5. 日期 CSV 选择：缺日期时不再全扫
# ============================================================


@dataclass
class DateCSVSelection:
    selected_files: List[str]
    required_dates: List[str]
    missing_dates: List[str]


class MissingDateCSVError(Exception):
    def __init__(self, required_dates: Sequence[str], missing_dates: Sequence[str]):
        self.required_dates = list(required_dates)
        self.missing_dates = list(missing_dates)
        msg = f"缺少日期CSV: {','.join(self.missing_dates)}；需要: {','.join(self.required_dates)}"
        super().__init__(msg)


def select_csv_files_by_time(data_dir: str | Path, read_start: pd.Timestamp, read_end: pd.Timestamp) -> DateCSVSelection:
    """
    根据读取窗口选择日期 CSV。

    最终版逻辑：
    - 只匹配读取窗口实际覆盖的日期，例如 2026-06-08.csv / 2026-06-09.csv；
    - 如果缺少日期 CSV，直接返回 missing_dates，不再兜底扫描全部 CSV；
    - 文件名按前 10 位日期识别，兼容 2026-06-09.csv 或 2026-06-09_xxx.csv。
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        raise FileNotFoundError(f'数据目录不存在: {data_dir}')

    date_range = pd.date_range(read_start.normalize(), read_end.normalize(), freq='D')
    required_dates = [d.strftime('%Y-%m-%d') for d in date_range]

    date_to_files: Dict[str, List[str]] = {d: [] for d in required_dates}
    for f in os.listdir(data_dir):
        if not f.lower().endswith('.csv'):
            continue
        date_part = os.path.splitext(f)[0][:10]
        if date_part in date_to_files:
            date_to_files[date_part].append(f)

    missing_dates = [d for d in required_dates if len(date_to_files.get(d, [])) == 0]
    selected_files: List[str] = []
    for d in required_dates:
        selected_files.extend(sorted(date_to_files.get(d, [])))

    return DateCSVSelection(
        selected_files=sorted(selected_files),
        required_dates=required_dates,
        missing_dates=missing_dates,
    )


def read_local_raw_data(
    data_dir: str | Path,
    train: Any,
    fault_time_utc: pd.Timestamp,
    read_before_min: int = READ_BEFORE_MIN,
    read_after_min: int = READ_AFTER_MIN,
    chunksize: int = 500000,
    file_partition_time: Optional[pd.Timestamp] = None,
) -> Tuple[pd.DataFrame, DateCSVSelection]:
    """读取故障窗口原始数据。

    两套时间口径必须分开：
    - file_partition_time：减 8 小时之前的故障时间，用它的日期匹配 YYYY-MM-DD.csv；
    - fault_time_utc：减 8 小时之后的 UTC 时间，用它筛选 CSV 内 `_time` 记录。

    file_partition_time 为空时沿用旧调用口径（用 fault_time_utc 选文件）。若按原始日期
    找到的文件没有匹配记录，则自动回退到 UTC 日期文件，以兼容历史混合分区数据。
    """
    train = normalize_train(train)

    read_start = fault_time_utc - pd.Timedelta(minutes=read_before_min)
    read_end = fault_time_utc + pd.Timedelta(minutes=read_after_min)

    partition_center = (
        pd.Timestamp(file_partition_time)
        if file_partition_time is not None
        else pd.Timestamp(fault_time_utc)
    )
    partition_start = partition_center - pd.Timedelta(minutes=read_before_min)
    partition_end = partition_center + pd.Timedelta(minutes=read_after_min)

    need_cols = ['_time', '_value', 'code', 'equipInstance', 'ts_date', 'eventCode', 'status']

    def read_selection(current: DateCSVSelection) -> pd.DataFrame:
        rows: List[pd.DataFrame] = []
        for filename in current.selected_files:
            path = Path(data_dir) / filename
            try:
                reader = pd.read_csv(path, chunksize=chunksize)
            except Exception as e:
                print('读取失败:', filename, e)
                continue

            for chunk in reader:
                cols = [c for c in need_cols if c in chunk.columns]
                chunk = chunk[cols].copy()

                required = ['_time', '_value', 'code', 'equipInstance']
                if any(c not in chunk.columns for c in required):
                    continue

                chunk = chunk[chunk['equipInstance'].astype(str) == train].copy()
                if chunk.shape[0] == 0:
                    continue

                chunk['_time'] = pd.to_datetime(chunk['_time'], errors='coerce')
                chunk['_value'] = pd.to_numeric(chunk['_value'], errors='coerce')
                chunk = chunk.dropna(subset=['_time', '_value', 'code'])

                chunk = chunk[(chunk['_time'] >= read_start) & (chunk['_time'] <= read_end)]
                if chunk.shape[0] > 0:
                    rows.append(chunk)

        if not rows:
            return pd.DataFrame(columns=need_cols)
        df = pd.concat(rows, ignore_index=True)
        return df.sort_values(['code', '_time']).reset_index(drop=True)

    # 第一优先级：按减 8 小时之前的原始故障日期选择 CSV。
    primary = select_csv_files_by_time(data_dir, partition_start, partition_end)
    df = pd.DataFrame(columns=need_cols)
    if not primary.missing_dates:
        df = read_selection(primary)
        if not df.empty:
            return df, primary

    # 兼容历史文件的 UTC 日期分区：主文件无匹配记录时才回退，不影响新规则优先级。
    fallback = select_csv_files_by_time(data_dir, read_start, read_end)
    if fallback.required_dates != primary.required_dates and not fallback.missing_dates:
        df = read_selection(fallback)
        if not df.empty:
            return df, fallback

    if primary.missing_dates:
        raise MissingDateCSVError(primary.required_dates, primary.missing_dates)
    return pd.DataFrame(columns=need_cols), primary


# ============================================================
# 6. 局部生成 events 和辅助特征
# ============================================================


def build_local_events_and_features(
    df_raw: pd.DataFrame,
    train: Any,
    fault_time_utc: pd.Timestamp,
    analyze_before_min: int = ANALYZE_BEFORE_MIN,
    analyze_after_min: int = ANALYZE_AFTER_MIN,
) -> Tuple[List[Tuple[Any, ...]], Dict[str, pd.DataFrame]]:
    train = normalize_train(train)

    analyze_start = fault_time_utc - pd.Timedelta(minutes=analyze_before_min)
    analyze_end = fault_time_utc + pd.Timedelta(minutes=analyze_after_min)

    events: List[Tuple[Any, ...]] = []
    feature_tables: Dict[str, pd.DataFrame] = {}

    if df_raw.shape[0] == 0:
        return events, feature_tables

    df_raw = df_raw.copy()
    df_raw['code_norm'] = df_raw['code'].astype(str).apply(normalize_code)
    df_raw['dim'] = df_raw['code_norm'].apply(to_dim)

    for code_norm, df_code in df_raw.groupby('code_norm'):
        dim = to_dim(code_norm)
        df_code = df_code.sort_values('_time')
        df_code = df_code.drop_duplicates(subset=['_time'], keep='last')
        df_code = df_code.dropna(subset=['_value'])

        if df_code.shape[0] == 0:
            continue

        # distance：不参与切 segment，只作为辅助特征
        if is_ramping_finish_code(code_norm):
            temp_feat = df_code[['_time', '_value']].copy()
            temp_feat = temp_feat.rename(columns={'_time': 'time', '_value': 'raw_value'})
            temp_feat = temp_feat.sort_values('time')
            temp_feat = temp_feat[
                (temp_feat['time'] >= analyze_start - pd.Timedelta(seconds=NEAREST_FEATURE_TOLERANCE_SECONDS)) &
                (temp_feat['time'] <= analyze_end + pd.Timedelta(seconds=NEAREST_FEATURE_TOLERANCE_SECONDS))
            ]
            if temp_feat.shape[0] > 0:
                temp_feat = temp_feat.drop_duplicates(subset=['time'], keep='last')
                temp_feat = temp_feat.set_index('time').sort_index()
                feature_tables[dim] = temp_feat

        if is_distance_code(code_norm):
            temp = df_code[['_time', '_value']].copy()
            temp = temp.rename(columns={'_time': 'time', '_value': 'raw'})
            temp = temp.sort_values('time')
            temp['delta'] = temp['raw'].diff().fillna(0)
            temp = temp[
                (temp['time'] >= analyze_start - pd.Timedelta(seconds=NEAREST_FEATURE_TOLERANCE_SECONDS)) &
                (temp['time'] <= analyze_end + pd.Timedelta(seconds=NEAREST_FEATURE_TOLERANCE_SECONDS))
            ]
            if temp.shape[0] > 0:
                temp = temp.drop_duplicates(subset=['time'], keep='last')
                temp = temp.set_index('time').sort_index()
                feature_tables[dim] = temp
            continue

        # 145：不参与切 segment，只作为辅助特征
        if is_145_code(code_norm):
            temp = df_code[['_time', '_value']].copy()
            temp = temp.rename(columns={'_time': 'time', '_value': 'raw_value'})
            temp = temp.sort_values('time')
            temp['delta'] = temp['raw_value'].diff().fillna(0)
            temp['value_bin'] = temp['raw_value'].apply(bin_145_value)
            temp['delta_bin'] = temp['delta'].apply(bin_145_delta)
            temp = temp[
                (temp['time'] >= analyze_start - pd.Timedelta(seconds=NEAREST_FEATURE_TOLERANCE_SECONDS)) &
                (temp['time'] <= analyze_end + pd.Timedelta(seconds=NEAREST_FEATURE_TOLERANCE_SECONDS))
            ]
            if temp.shape[0] > 0:
                temp = temp.drop_duplicates(subset=['time'], keep='last')
                temp = temp.set_index('time').sort_index()
                feature_tables[dim] = temp
            continue

        # 普通变量，包括 464，生成变化事件
        temp = df_code[['_time', '_value']].copy()
        temp = temp.rename(columns={'_time': 'time', '_value': 'value'})
        temp = temp.sort_values('time')
        temp = temp.drop_duplicates(subset=['time'], keep='last')
        temp['prev_time'] = temp['time'].shift(1)
        temp['prev_value'] = temp['value'].shift(1)
        temp = temp.dropna(subset=['prev_time', 'prev_value'])
        temp = temp[(temp['time'] >= analyze_start) & (temp['time'] <= analyze_end)]

        if temp.shape[0] == 0:
            continue

        # 464：复刻旧版 process464 逻辑，按 cur - prev 的变化幅度归并
        if is_464_code(code_norm):
            for _, row in temp.iterrows():
                try:
                    event = process_464_event(
                        train=train,
                        dim=dim,
                        prev_time=row['prev_time'],
                        prev_value=row['prev_value'],
                        cur_time=row['time'],
                        cur_value=row['value'],
                    )
                except Exception:
                    event = None

                if event is not None:
                    events.append(event)
            continue

        # 普通变量：值变化就作为事件
        temp_change = temp[temp['value'] != temp['prev_value']]
        for _, row in temp_change.iterrows():
            events.append((train, dim, row['prev_time'], row['prev_value'], row['time'], row['value']))

    events.sort(key=lambda x: x[4])
    return events, feature_tables


# ============================================================
# 7. segment 切分和选择
# ============================================================


def split_events_to_segments(events: Sequence[Tuple[Any, ...]]) -> List[List[Tuple[Any, ...]]]:
    if len(events) == 0:
        return []

    events = sorted(events, key=lambda x: x[4])
    split_points = []
    for i in range(1, len(events)):
        gap = (events[i][4] - events[i - 1][4]).total_seconds()
        if gap >= SEG_GAP_SECONDS:
            split_points.append(i)

    if len(split_points) == 0:
        return [list(events)]

    segs = (
        [events[0:split_points[0]]] +
        [events[split_points[i - 1]:split_points[i]] for i in range(1, len(split_points))] +
        [events[split_points[-1]:]]
    )
    return [list(seg) for seg in segs]


def get_segment_time_range(seg: Sequence[Tuple[Any, ...]]) -> Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    times: List[pd.Timestamp] = []
    for rcd in seg:
        try:
            times.append(pd.to_datetime(rcd[2]))
            times.append(pd.to_datetime(rcd[4]))
        except Exception:
            continue

    if len(times) == 0:
        return None, None
    return min(times), max(times)


def get_segment_center_time(seg: Sequence[Tuple[Any, ...]]) -> Optional[pd.Timestamp]:
    st, ed = get_segment_time_range(seg)
    if st is None or ed is None:
        return None
    return st + (ed - st) / 2


def segment_has_target(seg: Sequence[Tuple[Any, ...]], car_no: Any = None) -> bool:
    target_dims = get_vcb_target_dims_for_car(car_no)
    return any(rcd[1] in target_dims for rcd in seg)


def _event_value_is(value: Any, expected: float) -> bool:
    try:
        return float(value) == float(expected)
    except Exception:
        return str(value).strip() == str(int(expected))


def _segment_vcb_transition_count(seg: Sequence[Tuple[Any, ...]], prev_value: float, cur_value: float, car_no: Any = None) -> int:
    count = 0
    target_dims = get_vcb_target_dims_for_car(car_no)
    for rcd in seg:
        if len(rcd) < 6:
            continue
        if rcd[1] not in target_dims:
            continue
        if _event_value_is(rcd[3], prev_value) and _event_value_is(rcd[5], cur_value):
            count += 1
    return count


def segment_vcb_1_to_0_count(seg: Sequence[Tuple[Any, ...]], car_no: Any = None) -> int:
    return _segment_vcb_transition_count(seg, 1, 0, car_no=car_no)


def segment_vcb_0_to_1_count(seg: Sequence[Tuple[Any, ...]], car_no: Any = None) -> int:
    return _segment_vcb_transition_count(seg, 0, 1, car_no=car_no)


def find_nearest_row(df: pd.DataFrame, center_time: Optional[pd.Timestamp], tolerance_seconds: int) -> Optional[Tuple[pd.Timestamp, pd.Series, float]]:
    if df is None or len(df) == 0 or center_time is None:
        return None

    idx = df.index
    pos = idx.searchsorted(center_time)
    candidate_positions = []
    if pos < len(idx):
        candidate_positions.append(pos)
    if pos > 0:
        candidate_positions.append(pos - 1)
    if len(candidate_positions) == 0:
        return None

    best_pos = None
    best_diff = None
    for p in candidate_positions:
        t = idx[p]
        diff = abs((t - center_time).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_pos = p

    if best_pos is None or best_diff is None:
        return None
    if best_diff > tolerance_seconds:
        return None

    t = idx[best_pos]
    row = df.iloc[best_pos]
    return t, row, float(best_diff)


def get_segment_transition_ramping_refs(seg: Sequence[Tuple[Any, ...]], prev_value: float, cur_value: float, car_no: Any = None) -> Dict[str, List[pd.Timestamp]]:
    refs: Dict[str, List[pd.Timestamp]] = {}
    target_dims = get_vcb_target_dims_for_car(car_no)
    for rcd in seg:
        if len(rcd) < 6:
            continue
        if rcd[1] not in target_dims:
            continue
        if not (_event_value_is(rcd[3], prev_value) and _event_value_is(rcd[5], cur_value)):
            continue
        ramping_dim = vcb_to_ramping_finish_dim.get(str(rcd[1]))
        if ramping_dim is None:
            continue
        try:
            event_time = pd.to_datetime(rcd[4])
        except Exception:
            continue
        refs.setdefault(ramping_dim, []).append(event_time)

    return refs


def get_ramping_finish_reference_times(seg: Sequence[Tuple[Any, ...]], car_no: Any = None) -> Dict[str, List[pd.Timestamp]]:
    return get_segment_transition_ramping_refs(seg, 1, 0, car_no=car_no)


def ramping_finish_status_value(raw_value: Any) -> int:
    try:
        return 1 if float(raw_value) == 1.0 else 0
    except Exception:
        return 1 if str(raw_value).strip() == '1' else 0


def add_aux_features_to_segment(seg: Sequence[Tuple[Any, ...]], feature_tables: Dict[str, pd.DataFrame], train: Any, car_no: Any = None) -> List[Tuple[Any, ...]]:
    train = normalize_train(train)
    center_time = get_segment_center_time(seg)
    ramping_finish_ref_times = get_ramping_finish_reference_times(seg, car_no=car_no)
    new_seg = list(seg)

    for dim, df_feat in feature_tables.items():
        if is_ramping_finish_code(dim):
            ref_times = ramping_finish_ref_times.get(str(dim), [])
            if not ref_times:
                continue
            for ref_time in ref_times:
                if ref_time not in df_feat.index:
                    continue
                row = df_feat.loc[ref_time]
                status_value = ramping_finish_status_value(row['raw_value'])
                new_seg.append((train, dim, ref_time, 'ramping_finish_status', ref_time, status_value))
            continue
        else:
            ref_time = center_time
        result = find_nearest_row(df_feat, ref_time, NEAREST_FEATURE_TOLERANCE_SECONDS)
        if result is None:
            continue

        t, row, _ = result
        if is_distance_code(dim):
            raw_bin, delta_bin = discretize_distance(row['raw'], row['delta'])
            new_seg.append((train, dim, t, raw_bin, t, delta_bin))
        elif is_145_code(dim):
            new_seg.append((train, dim, t, row['value_bin'], t, row['delta_bin']))

    return new_seg


def select_main_segment(segs: Sequence[Sequence[Tuple[Any, ...]]], fault_time_utc: pd.Timestamp, keep_top_k: int = 5, car_no: Any = None) -> Tuple[Optional[List[Tuple[Any, ...]]], pd.DataFrame]:
    rows: List[Dict[str, Any]] = []
    for idx, seg in enumerate(segs):
        st, ed = get_segment_time_range(seg)
        center = get_segment_center_time(seg)
        if center is None:
            continue
        signed_diff_sec = float((center - fault_time_utc).total_seconds())
        abs_diff_sec = abs(signed_diff_sec)
        vcb_1_to_0_count = segment_vcb_1_to_0_count(seg, car_no=car_no)
        vcb_0_to_1_count = segment_vcb_0_to_1_count(seg, car_no=car_no)
        rows.append({
            'local_seg_idx': idx,
            'start_time': st,
            'end_time': ed,
            'center_time': center,
            'center_minus_fault_seconds': signed_diff_sec,
            'time_diff_seconds': abs_diff_sec,
            'event_count': len(seg),
            'has_target': segment_has_target(seg, car_no=car_no),
            'vcb_1_to_0_count': vcb_1_to_0_count,
            'vcb_0_to_1_count': vcb_0_to_1_count,
            'has_vcb_1_to_0': vcb_1_to_0_count > 0,
            'has_vcb_0_to_1': vcb_0_to_1_count > 0,
        })

    if len(rows) == 0:
        return None, pd.DataFrame()

    df_candidates = pd.DataFrame(rows)

    def choose_by_time_rule(
        df_pool: pd.DataFrame,
        allow_extended_after: bool = False,
    ) -> Tuple[Optional[pd.Series], str]:
        if df_pool.empty:
            return None, ''

        before_near = df_pool[
            (df_pool['center_minus_fault_seconds'] <= 0) &
            (df_pool['time_diff_seconds'] <= MAIN_SEGMENT_BEFORE_SECONDS)
        ].copy()
        if len(before_near) > 0:
            return before_near.sort_values(
                ['center_minus_fault_seconds', 'time_diff_seconds'],
                ascending=[False, True],
            ).iloc[0], 'before_within_120s'

        after_fallback = df_pool[
            (df_pool['center_minus_fault_seconds'] > 0) &
            (df_pool['center_minus_fault_seconds'] <= MAIN_SEGMENT_AFTER_FALLBACK_SECONDS)
        ].copy()
        if len(after_fallback) > 0:
            return after_fallback.sort_values('center_minus_fault_seconds').iloc[0], 'after_within_30s_fallback'

        if allow_extended_after:
            before_guard = df_pool[
                (df_pool['center_minus_fault_seconds'] <= 0) &
                (df_pool['time_diff_seconds'] <= MAIN_SEGMENT_BEFORE_GUARD_SECONDS)
            ]
            if before_guard.empty:
                after_extended = df_pool[
                    (df_pool['center_minus_fault_seconds'] > 0) &
                    (df_pool['center_minus_fault_seconds'] <= MAIN_SEGMENT_AFTER_EXTENDED_FALLBACK_SECONDS)
                ].copy()
                if len(after_extended) > 0:
                    return (
                        after_extended.sort_values('center_minus_fault_seconds').iloc[0],
                        'after_within_5min_no_vcb_1_to_0_before_10min',
                    )

        before_far = df_pool[
            (df_pool['center_minus_fault_seconds'] < -MAIN_SEGMENT_BEFORE_SECONDS) &
            (df_pool['time_diff_seconds'] <= MAIN_SEGMENT_MAX_ABS_SECONDS)
        ].copy()
        if len(before_far) > 0:
            return before_far.sort_values(
                ['center_minus_fault_seconds', 'time_diff_seconds'],
                ascending=[False, True],
            ).iloc[0], 'before_120_to_180s'

        return None, 'no_segment_within_time_rule'

    selected = None
    selection_rule = ''
    selection_transition = ''
    selection_note = ''

    df_1_to_0 = df_candidates[df_candidates['has_vcb_1_to_0']].copy()
    if len(df_1_to_0) > 0:
        selected, selection_rule = choose_by_time_rule(df_1_to_0, allow_extended_after=True)
        selection_transition = 'vcb_1_to_0'
        if selected is None:
            selection_note = 'has_vcb_1_to_0_but_no_segment_within_time_rule'
    else:
        df_0_to_1 = df_candidates[df_candidates['has_vcb_0_to_1']].copy()
        selected, selection_rule = choose_by_time_rule(df_0_to_1, allow_extended_after=True)
        selection_transition = 'vcb_0_to_1_fallback'
        selection_note = 'no_vcb_1_to_0_in_case; fallback_to_vcb_0_to_1'
        if selected is None:
            selection_note = 'no_vcb_1_to_0_in_case; no_vcb_0_to_1_segment_within_time_rule'

    df_candidates['selected_as_main'] = False
    df_candidates['selection_transition'] = selection_transition
    df_candidates['selection_rule'] = selection_rule
    df_candidates['selection_note'] = selection_note

    if selected is None:
        df_candidates = df_candidates.sort_values('time_diff_seconds').reset_index(drop=True)
        return None, df_candidates.head(keep_top_k).copy()

    main_idx = int(selected['local_seg_idx'])
    df_candidates.loc[df_candidates['local_seg_idx'] == main_idx, 'selected_as_main'] = True
    df_candidates = df_candidates.sort_values(
        ['selected_as_main', 'time_diff_seconds'],
        ascending=[False, True],
    ).reset_index(drop=True)
    return list(segs[main_idx]), df_candidates.head(keep_top_k).copy()


# ============================================================
# 8. 向量化
# ============================================================


def segment_to_node_counts(seg: Sequence[Tuple[Any, ...]], car_no: Any = None) -> Dict[str, int]:
    node_counts: Dict[str, int] = {}
    allowed_vcb_dims = get_vcb_target_dims_for_car(car_no)
    allowed_ramping_dims = get_ramping_finish_dims_for_car(car_no)
    for rcd in seg:
        if len(rcd) < 6:
            continue
        dim = str(rcd[1])
        if dim in set(target_column_comb) and dim not in allowed_vcb_dims:
            continue
        if is_ramping_finish_code(dim) and dim not in allowed_ramping_dims:
            continue
        normalized_dim = target_car_normalized_dim(dim, car_no=car_no)
        if normalized_dim is None:
            continue
        node = str(normalized_dim) + '_' + str(rcd[3]) + '_' + str(rcd[5])
        node_counts[node] = node_counts.get(node, 0) + 1
    return node_counts


def build_node_index(node_counts_list: Sequence[Dict[str, int]]) -> Dict[str, int]:
    all_nodes = set()
    for node_counts in node_counts_list:
        all_nodes.update(node_counts.keys())
    nodes = sorted(list(all_nodes))
    return {node: idx for idx, node in enumerate(nodes)}


def vectorize_node_counts(node_counts: Dict[str, int], node_index: Dict[str, int]) -> Tuple[np.ndarray, Dict[str, int]]:
    vec = np.zeros(len(node_index), dtype=float)
    unknown_nodes: Dict[str, int] = {}

    for node, cnt in node_counts.items():
        if node in node_index:
            vec[node_index[node]] += cnt
        else:
            unknown_nodes[node] = cnt

    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec, unknown_nodes


# ============================================================
# 9. 单个工单：生成 segment 和 node_counts
# ============================================================


def get_local_case_result(
    train: Any,
    fault_time: Any,
    car_no: Any = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    time_is_cst: bool = True,
    read_before_min: int = READ_BEFORE_MIN,
    read_after_min: int = READ_AFTER_MIN,
    analyze_before_min: int = ANALYZE_BEFORE_MIN,
    analyze_after_min: int = ANALYZE_AFTER_MIN,
    keep_top_k: int = 5,
) -> Dict[str, Any]:
    train = normalize_train(train)
    car_no = normalize_car_no(car_no)
    file_partition_time = pd.to_datetime(fault_time)
    fault_time_utc = parse_time_to_utc(fault_time, time_is_cst=time_is_cst)

    try:
        df_raw, date_selection = read_local_raw_data(
            data_dir=data_dir,
            train=train,
            fault_time_utc=fault_time_utc,
            file_partition_time=file_partition_time,
            read_before_min=read_before_min,
            read_after_min=read_after_min,
        )
    except MissingDateCSVError as e:
        return {
            'status': 'no_date_csv',
            'train': train,
            'car_no': car_no,
            'fault_time_utc': fault_time_utc,
            'raw_rows': 0,
            'events': [],
            'segments_all': [],
            'segments_target': [],
            'main_segment': None,
            'candidate_table': pd.DataFrame(),
            'node_counts': {},
            'required_dates': e.required_dates,
            'missing_dates': e.missing_dates,
        }

    if df_raw.shape[0] == 0:
        return {
            'status': 'no_raw_data',
            'train': train,
            'car_no': car_no,
            'fault_time_utc': fault_time_utc,
            'raw_rows': 0,
            'events': [],
            'segments_all': [],
            'segments_target': [],
            'main_segment': None,
            'candidate_table': pd.DataFrame(),
            'node_counts': {},
            'required_dates': date_selection.required_dates,
            'missing_dates': date_selection.missing_dates,
        }

    events, feature_tables = build_local_events_and_features(
        df_raw=df_raw,
        train=train,
        fault_time_utc=fault_time_utc,
        analyze_before_min=analyze_before_min,
        analyze_after_min=analyze_after_min,
    )

    segs_all = split_events_to_segments(events)
    segs_target = [seg for seg in segs_all if segment_has_target(seg, car_no=car_no)]
    segs_target = [add_aux_features_to_segment(seg, feature_tables, train, car_no=car_no) for seg in segs_target]

    if len(segs_target) == 0:
        return {
            'status': 'no_target_segment',
            'train': train,
            'car_no': car_no,
            'fault_time_utc': fault_time_utc,
            'raw_rows': len(df_raw),
            'events': events,
            'segments_all': segs_all,
            'segments_target': [],
            'main_segment': None,
            'candidate_table': pd.DataFrame(),
            'node_counts': {},
            'required_dates': date_selection.required_dates,
            'missing_dates': date_selection.missing_dates,
        }

    main_segment, candidate_table = select_main_segment(segs_target, fault_time_utc, keep_top_k=keep_top_k, car_no=car_no)
    if main_segment is None:
        return {
            'status': 'no_valid_main_segment',
            'train': train,
            'car_no': car_no,
            'fault_time_utc': fault_time_utc,
            'raw_rows': len(df_raw),
            'events': events,
            'segments_all': segs_all,
            'segments_target': segs_target,
            'main_segment': None,
            'candidate_table': candidate_table,
            'node_counts': {},
            'selection_transition': '',
            'selection_rule': '',
            'selection_note': 'no segment matched the VCB transition/time selection rule',
            'required_dates': date_selection.required_dates,
            'missing_dates': date_selection.missing_dates,
        }

    node_counts = segment_to_node_counts(main_segment, car_no=car_no)
    selected_row = candidate_table.iloc[0] if candidate_table is not None and len(candidate_table) > 0 else {}
    return {
        'status': 'ok',
        'train': train,
        'car_no': car_no,
        'fault_time_utc': fault_time_utc,
        'raw_rows': len(df_raw),
        'events': events,
        'segments_all': segs_all,
        'segments_target': segs_target,
        'main_segment': main_segment,
        'candidate_table': candidate_table,
        'node_counts': node_counts,
        'selection_transition': selected_row.get('selection_transition', ''),
        'selection_rule': selected_row.get('selection_rule', ''),
        'selection_note': selected_row.get('selection_note', ''),
        'selected_center_minus_fault_seconds': selected_row.get('center_minus_fault_seconds', ''),
        'selected_vcb_1_to_0_count': selected_row.get('vcb_1_to_0_count', ''),
        'selected_vcb_0_to_1_count': selected_row.get('vcb_0_to_1_count', ''),
        'required_dates': date_selection.required_dates,
        'missing_dates': date_selection.missing_dates,
    }


# ============================================================
# 10. case_input 读取与工单处理
# ============================================================


def load_case_input_for_build(case_csv: str | Path, desc_csv: Optional[str | Path] = None) -> pd.DataFrame:
    case_df = pd.read_csv(case_csv, dtype=str, encoding='utf-8-sig')
    case_df.columns = [str(c).strip() for c in case_df.columns]

    case_id_col = pick_col(case_df, CASE_ID_ALIASES, name='case_csv工单号')
    train_col = pick_col(case_df, TRAIN_ALIASES, name='case_csv车号/车组')
    time_col = pick_col(case_df, FAULT_TIME_ALIASES, name='case_csv故障时间')

    car_col = pick_col(case_df, CAR_NO_ALIASES, required=False, name='case_csv CarNO')

    case_df = case_df.rename(columns={
        case_id_col: 'work_order_no',
        train_col: 'train',
        time_col: 'fault_time',
    })
    if car_col is not None:
        case_df = case_df.rename(columns={car_col: 'car_no'})
    case_df = case_df.loc[:, ~case_df.columns.duplicated()].copy()

    case_df['work_order_no'] = case_df['work_order_no'].astype(str).str.strip()
    case_df['train'] = case_df['train'].astype(str).str.strip().apply(normalize_train)
    if 'car_no' not in case_df.columns:
        case_df['car_no'] = ''
    case_df['car_no'] = case_df['car_no'].fillna('').astype(str).apply(normalize_car_no)
    case_df['fault_time'] = case_df['fault_time'].fillna('').astype(str).str.strip()

    if 'time_order' in case_df.columns:
        case_df['time_order'] = pd.to_numeric(case_df['time_order'], errors='coerce')
        case_df['time_order'] = case_df['time_order'].fillna(case_df.groupby('work_order_no').cumcount()).astype(int)
    else:
        case_df['time_order'] = case_df.groupby('work_order_no').cumcount()

    if 'remark' not in case_df.columns:
        case_df['remark'] = ''
    case_df['remark'] = case_df['remark'].fillna('').astype(str)

    if desc_csv is not None and str(desc_csv).strip() != '':
        desc_df = pd.read_csv(desc_csv, dtype=str, encoding='utf-8-sig')
        desc_df.columns = [str(c).strip() for c in desc_df.columns]
        desc_id_col = pick_col(desc_df, CASE_ID_ALIASES, name='desc_csv工单号')
        longdesc_col = pick_col(desc_df, DESC_TEXT_ALIASES, name='desc_csv LongDescription')
        desc_df = desc_df.rename(columns={desc_id_col: 'work_order_no', longdesc_col: 'long_description'})
        desc_df = desc_df.loc[:, ~desc_df.columns.duplicated()].copy()
        desc_df['work_order_no'] = desc_df['work_order_no'].astype(str).str.strip()
        desc_df['long_description'] = desc_df['long_description'].fillna('').astype(str)
        desc_df = desc_df[['work_order_no', 'long_description']].drop_duplicates(subset=['work_order_no'], keep='first')
        case_df = case_df.merge(desc_df, on='work_order_no', how='left')
        case_df['long_description'] = case_df['long_description'].fillna('')
    else:
        case_df['long_description'] = ''

    dup_total = case_df.groupby('work_order_no')['work_order_no'].transform('count')
    case_df['case_id'] = case_df['work_order_no']
    mask_dup = dup_total > 1
    case_df.loc[mask_dup, 'case_id'] = (
        case_df.loc[mask_dup, 'work_order_no'].astype(str) + '__dup' + case_df.loc[mask_dup, 'time_order'].astype(str)
    )

    print('case_csv 行数:', len(case_df))
    print('使用 desc_csv:', desc_csv if desc_csv else '否，LongDescription 留空')
    return case_df


def process_work_df(work_df: pd.DataFrame, data_dir: str | Path, time_is_cst: bool = True) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Dict[str, int]], List[Dict[str, Any]]]:
    ok_records: List[Dict[str, Any]] = []
    process_logs: List[Dict[str, Any]] = []
    segments_store: Dict[str, Any] = {}
    node_counts_store: Dict[str, Dict[str, int]] = {}
    missing_date_rows: List[Dict[str, Any]] = []

    for _, row in tqdm(work_df.iterrows(), total=len(work_df), desc='处理工单'):
        case_id = str(row['case_id'])
        work_order_no = str(row['work_order_no'])
        train = row['train']
        car_no = normalize_car_no(row.get('car_no', ''))
        fault_time = row['fault_time']
        time_order = int(row.get('time_order', 0)) if safe_str(row.get('time_order', '')) != '' else 0
        remark = safe_str(row.get('remark', ''))
        long_description = safe_str(row.get('long_description', ''))

        fault_time_text = safe_str(fault_time)
        if fault_time_text == '' or fault_time_text.lower() in ['nan', 'nat', 'none', 'null']:
            process_logs.append({
                'case_id': case_id,
                'work_order_no': work_order_no,
                'time_order': time_order,
                'remark': remark,
                'train': normalize_train(train),
                'car_no': car_no,
                'fault_time_raw': '',
                'fault_time_utc': '',
                'status': 'no_explicit_fault_time',
                'raw_rows': 0,
                'event_count': 0,
                'segments_all_count': 0,
                'segments_target_count': 0,
                'required_dates': '',
                'missing_dates': '',
                'error': '',
            })
            continue

        try:
            result = get_local_case_result(
                train=train,
                fault_time=fault_time,
                car_no=car_no,
                data_dir=data_dir,
                time_is_cst=time_is_cst,
            )

            status = result['status']
            required_dates = ';'.join(result.get('required_dates', []) or [])
            missing_dates = ';'.join(result.get('missing_dates', []) or [])
            selection_transition = result.get('selection_transition', '')
            selection_rule = result.get('selection_rule', '')
            selection_note = result.get('selection_note', '')
            segment_selection_remark = selection_note
            if selection_transition == 'vcb_0_to_1_fallback':
                segment_selection_remark = 'no VCB 1->0 transition in this case window; fallback to VCB 0->1 segment'

            log_item = {
                'case_id': case_id,
                'work_order_no': work_order_no,
                'time_order': time_order,
                'remark': remark,
                'train': normalize_train(train),
                'car_no': car_no,
                'fault_time_raw': fault_time,
                'fault_time_utc': result.get('fault_time_utc', ''),
                'status': status,
                'raw_rows': result.get('raw_rows', 0),
                'event_count': len(result.get('events', [])),
                'segments_all_count': len(result.get('segments_all', [])),
                'segments_target_count': len(result.get('segments_target', [])),
                'selection_transition': selection_transition,
                'selection_rule': selection_rule,
                'selection_note': selection_note,
                'segment_selection_remark': segment_selection_remark,
                'required_dates': required_dates,
                'missing_dates': missing_dates,
                'error': '',
            }

            if status == 'no_date_csv':
                missing_date_rows.append({
                    'case_id': case_id,
                    'work_order_no': work_order_no,
                    'time_order': time_order,
                    'remark': remark,
                    'train': normalize_train(train),
                    'car_no': car_no,
                    'fault_time_raw': fault_time,
                    'fault_time_utc': result.get('fault_time_utc', ''),
                    'required_dates': required_dates,
                    'missing_dates': missing_dates,
                    'status': status,
                })

            if status == 'ok':
                main_segment = result['main_segment']
                seg_start, seg_end = get_segment_time_range(main_segment)
                seg_center = get_segment_center_time(main_segment)
                candidate_table = result['candidate_table']
                time_diff_seconds = None
                if candidate_table is not None and len(candidate_table) > 0:
                    time_diff_seconds = candidate_table.iloc[0]['time_diff_seconds']

                node_counts = result['node_counts']
                meta = {
                    'case_id': case_id,
                    'work_order_no': work_order_no,
                    'time_order': time_order,
                    'remark': remark,
                    'row_index': -1,
                    'train': normalize_train(train),
                    'car_no': car_no,
                    'fault_time_raw': fault_time,
                    'fault_time_utc': result['fault_time_utc'],
                    'segment_start': seg_start,
                    'segment_end': seg_end,
                    'segment_center': seg_center,
                    'time_diff_seconds': time_diff_seconds,
                    'center_minus_fault_seconds': result.get('selected_center_minus_fault_seconds', ''),
                    'selection_transition': selection_transition,
                    'selection_rule': selection_rule,
                    'selection_note': selection_note,
                    'segment_selection_remark': segment_selection_remark,
                    'selected_vcb_1_to_0_count': result.get('selected_vcb_1_to_0_count', ''),
                    'selected_vcb_0_to_1_count': result.get('selected_vcb_0_to_1_count', ''),
                    'event_count': len(main_segment),
                    'node_count': len(node_counts),
                    'raw_rows': result.get('raw_rows', 0),
                    'long_description': long_description,
                }
                ok_records.append(meta)
                segments_store[case_id] = main_segment
                node_counts_store[case_id] = node_counts

            process_logs.append(log_item)

        except Exception as e:
            process_logs.append({
                'case_id': case_id,
                'work_order_no': work_order_no,
                'time_order': time_order,
                'remark': remark,
                'train': normalize_train(train),
                'car_no': car_no,
                'fault_time_raw': fault_time,
                'fault_time_utc': '',
                'status': 'error',
                'raw_rows': 0,
                'event_count': 0,
                'segments_all_count': 0,
                'segments_target_count': 0,
                'required_dates': '',
                'missing_dates': '',
                'error': str(e),
            })

    return ok_records, process_logs, segments_store, node_counts_store, missing_date_rows


def extract_fault_code_for_gateway(row: pd.Series | Dict[str, Any]) -> str:
    """从工单备注/描述提取用于下载清单追踪的 P 故障码。"""
    text = ' '.join([
        safe_str(row.get('remark', '')),
        safe_str(row.get('long_description', '')),
    ])
    match = re.search(r'\bP\s*0*(\d+)\b', text, flags=re.IGNORECASE)
    if not match:
        return ''
    return f"P{int(match.group(1)):03d}"


def _gateway_error_log(row: pd.Series, error: Exception) -> Dict[str, Any]:
    fault_time = row.get('fault_time', '')
    return {
        'case_id': str(row.get('case_id', '')),
        'work_order_no': str(row.get('work_order_no', '')),
        'time_order': row.get('time_order', 0),
        'remark': safe_str(row.get('remark', '')),
        'train': normalize_train(row.get('train', '')),
        'car_no': normalize_car_no(row.get('car_no', '')),
        'fault_time_raw': fault_time,
        'fault_time_utc': '',
        'status': 'gateway_download_error',
        'raw_rows': 0,
        'event_count': 0,
        'segments_all_count': 0,
        'segments_target_count': 0,
        'required_dates': '',
        'missing_dates': '',
        'data_source': 'gateway_error',
        'gateway_data_dir': '',
        'gateway_reused': False,
        'gateway_metric_count': 0,
        'gateway_row_count': 0,
        'gateway_error': str(error),
        'error': str(error),
    }


def process_work_df_with_gateway(
    work_df: pd.DataFrame,
    data_dir: str | Path,
    time_is_cst: bool = True,
    gateway_output_root: str | Path = DEFAULT_GATEWAY_WORK_ORDER_ROOT,
    gateway_endpoint: Optional[str] = None,
    gateway_metrics_json: Optional[str] = None,
    gateway_batch_size: int = 25,
    fallback_to_local_data: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Dict[str, int]], List[Dict[str, Any]]]:
    """逐个真实工单下载故障窗口，再复用现有逻辑生成案例向量。"""
    from gateway_download import download_fault_window

    ok_records: List[Dict[str, Any]] = []
    process_logs: List[Dict[str, Any]] = []
    segments_store: Dict[str, Any] = {}
    node_counts_store: Dict[str, Dict[str, int]] = {}
    missing_date_rows: List[Dict[str, Any]] = []

    for _, row in tqdm(work_df.iterrows(), total=len(work_df), desc='网关下载并处理工单'):
        one_row = pd.DataFrame([row.to_dict()])
        fault_time_text = safe_str(row.get('fault_time', ''))
        download_result = None
        gateway_error: Exception | None = None

        if fault_time_text and fault_time_text.lower() not in ['nan', 'nat', 'none', 'null']:
            try:
                download_result = download_fault_window(
                    train=row.get('train', ''),
                    fault_time=fault_time_text,
                    fault_code=extract_fault_code_for_gateway(row),
                    time_is_cst=time_is_cst,
                    before_min=READ_BEFORE_MIN,
                    after_min=READ_AFTER_MIN,
                    endpoint=gateway_endpoint,
                    metrics_json=gateway_metrics_json,
                    metrics_data_dir=str(data_dir),
                    output_root=gateway_output_root,
                    batch_size=gateway_batch_size,
                    reuse=True,
                )
            except Exception as exc:
                gateway_error = exc
                print(
                    f"网关下载失败: work_order={row.get('work_order_no', '')}, "
                    f"fault_time={fault_time_text}, error={exc}"
                )

        if gateway_error is not None and not fallback_to_local_data:
            process_logs.append(_gateway_error_log(row, gateway_error))
            continue

        current_data_dir = download_result.data_dir if download_result is not None else data_dir
        current_source = (
            'gateway_cache' if download_result is not None and download_result.reused
            else 'gateway'
            if download_result is not None
            else 'local_fallback'
            if gateway_error is not None
            else 'no_fault_time'
        )
        current = process_work_df(one_row, data_dir=current_data_dir, time_is_cst=time_is_cst)
        row_ok, row_logs, row_segments, row_counts, row_missing = current

        trace_fields = {
            'data_source': current_source,
            'gateway_data_dir': str(download_result.data_dir) if download_result is not None else '',
            'gateway_reused': bool(download_result.reused) if download_result is not None else False,
            'gateway_metric_count': int(download_result.metric_count) if download_result is not None else 0,
            'gateway_row_count': int(download_result.row_count) if download_result is not None else 0,
            'gateway_error': str(gateway_error) if gateway_error is not None else '',
        }
        for record in row_ok:
            record.update(trace_fields)
        for record in row_logs:
            record.update(trace_fields)
        for record in row_missing:
            record.update(trace_fields)

        ok_records.extend(row_ok)
        process_logs.extend(row_logs)
        segments_store.update(row_segments)
        node_counts_store.update(row_counts)
        missing_date_rows.extend(row_missing)

    return ok_records, process_logs, segments_store, node_counts_store, missing_date_rows


# ============================================================
# 11. 保存 / 重建案例库
# ============================================================


def rebuild_vectors_from_metadata_and_counts(metadata_df: pd.DataFrame, node_counts_store: Dict[str, Dict[str, int]]) -> Tuple[pd.DataFrame, np.ndarray, Dict[str, int]]:
    metadata_df = metadata_df.copy().reset_index(drop=True)
    node_index = build_node_index(list(node_counts_store.values()))
    vectors = np.zeros((len(metadata_df), len(node_index)), dtype=float)

    for row_idx, meta_row in metadata_df.iterrows():
        case_id = str(meta_row['case_id'])
        node_counts = node_counts_store.get(case_id, {})
        vec, _ = vectorize_node_counts(node_counts, node_index)
        vectors[row_idx] = vec
        metadata_df.at[row_idx, 'row_index'] = row_idx

    return metadata_df, vectors, node_index


def save_case_library_files(
    save_dir: str | Path,
    metadata_df: pd.DataFrame,
    process_log_df: pd.DataFrame,
    segments_store: Dict[str, Any],
    node_counts_store: Dict[str, Dict[str, int]],
    data_dir: str | Path,
    case_csv: str | Path,
    time_is_cst: bool,
    missing_date_df: Optional[pd.DataFrame] = None,
    update_info: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, np.ndarray, Dict[str, int]]:
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    metadata_df, vectors, node_index = rebuild_vectors_from_metadata_and_counts(metadata_df, node_counts_store)

    np.save(save_dir / 'vectors.npy', vectors)
    metadata_df.to_csv(save_dir / 'metadata.csv', index=False, encoding='utf-8-sig')
    process_log_df.to_csv(save_dir / 'process_log.csv', index=False, encoding='utf-8-sig')
    joblib.dump(node_index, save_dir / 'node_index.joblib')
    joblib.dump(segments_store, save_dir / 'segments.joblib')
    joblib.dump(node_counts_store, save_dir / 'node_counts.joblib')

    if missing_date_df is not None and len(missing_date_df) > 0:
        missing_date_df.to_csv(save_dir / DEFAULT_MISSING_DATE_CSV, index=False, encoding='utf-8-sig')

    manifest = {
        'created_or_updated_at': datetime.now().isoformat(),
        'data_dir': os.path.abspath(str(data_dir)),
        'case_csv': os.path.abspath(str(case_csv)),
        'vector_shape': list(vectors.shape),
        'case_count_ok': int(len(metadata_df)),
        'case_count_total': int(len(process_log_df)),
        'time_is_cst': bool(time_is_cst),
        'read_before_min': READ_BEFORE_MIN,
        'read_after_min': READ_AFTER_MIN,
        'analyze_before_min': ANALYZE_BEFORE_MIN,
        'analyze_after_min': ANALYZE_AFTER_MIN,
        'seg_gap_seconds': SEG_GAP_SECONDS,
        'distance_bin_size': DISTANCE_BIN_SIZE,
        'nearest_feature_tolerance_seconds': NEAREST_FEATURE_TOLERANCE_SECONDS,
        'target_column': target_column,
        'ramping_finish_column': ramping_finish_column,
        'ramping_finish_aux_feature': 'only corresponding VCB circuit is sampled at VCB 1->0 time; fallback to VCB 0->1 time',
        'update_info': update_info or {},
    }
    with open(save_dir / 'manifest.json', 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    print('案例库保存完成')
    print('vectors:', vectors.shape)
    print('metadata:', save_dir / 'metadata.csv')
    print('process_log:', save_dir / 'process_log.csv')
    print('node_index:', save_dir / 'node_index.joblib')
    return metadata_df, vectors, node_index


# ============================================================
# 11.5 同步案例库到 Qdrant
# ============================================================


_QDRANT_PAYLOAD_EXCLUDE = ['tsne_1', 'tsne_2', 'vector_norm', 'nonzero_dim_count']


def _qdrant_clean_value(x: Any) -> Any:
    """Qdrant payload 不接受 NaN，统一转 None；numpy 类型转成 Python 原生类型。"""
    import math
    if pd.isna(x):
        return None
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        if math.isnan(float(x)):
            return None
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def _qdrant_existing_dim(info: Any) -> Optional[int]:
    """尽量从 get_collection 返回结果里读出现有 collection 的向量维度。"""
    try:
        vectors_cfg = info.config.params.vectors
        # 默认未命名向量：vectors_cfg 是 VectorParams，有 .size
        size = getattr(vectors_cfg, 'size', None)
        if size is not None:
            return int(size)
        # 命名向量：vectors_cfg 是 {name: VectorParams}
        if isinstance(vectors_cfg, dict) and len(vectors_cfg) == 1:
            return int(next(iter(vectors_cfg.values())).size)
    except Exception:
        return None
    return None


def sync_library_to_qdrant(
    library_dir: str | Path = DEFAULT_SAVE_DIR,
    qdrant_url: str = DEFAULT_QDRANT_URL,
    collection_name: str = DEFAULT_QDRANT_COLLECTION,
    recreate: bool = True,
    only_case_ids: Optional[Iterable[str]] = None,
) -> int:
    """把本地案例库 (vectors.npy + metadata.csv) 同步进 Qdrant。

    - recreate=True：删表重建后整库写入（维度变化、或想清理旧 point 时用）。
    - recreate=False：增量模式，不删表。
        * only_case_ids 为 None 时 upsert 全部 point；
        * only_case_ids 给定时只 upsert 这些 case_id（日常增量，最快）。
      增量模式会先校验现有 collection 维度与本地向量一致，不一致会报错提示重建。

    返回实际写入的 point 数量。
    """
    # 防止本地代理影响 localhost 连接
    os.environ.setdefault('NO_PROXY', 'localhost,127.0.0.1,10.0.1.87,::1')
    os.environ.setdefault('no_proxy', 'localhost,127.0.0.1,10.0.1.87,::1')

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, PointStruct
    except Exception as e:
        raise ImportError(
            '需要 qdrant-client 才能同步 Qdrant。请先 pip install qdrant-client，'
            '或在更新命令上加 --no-qdrant 跳过同步。'
        ) from e

    library_dir = Path(library_dir)
    vectors_path = library_dir / 'vectors.npy'
    metadata_path = library_dir / 'metadata.csv'
    if not vectors_path.exists() or not metadata_path.exists():
        raise FileNotFoundError(f'案例库文件不完整，找不到 vectors.npy 或 metadata.csv: {library_dir}')

    vectors = np.load(vectors_path).astype('float32')
    metadata = pd.read_csv(metadata_path)
    if len(metadata) != len(vectors):
        raise ValueError(f'metadata 行数({len(metadata)})和 vectors 行数({len(vectors)})不一致')

    vector_dim = int(vectors.shape[1])
    client = QdrantClient(url=qdrant_url)
    exists = client.collection_exists(collection_name)

    if recreate:
        if exists:
            print(f'删除旧 Qdrant collection: {collection_name}')
            client.delete_collection(collection_name)
        print(f'创建 Qdrant collection: {collection_name}, dim={vector_dim}')
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
        )
    elif not exists:
        print(f'Qdrant collection 不存在，创建: {collection_name}, dim={vector_dim}')
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
        )
    else:
        # 增量模式：维度必须一致，否则老向量已失效，必须重建
        existing_dim = _qdrant_existing_dim(client.get_collection(collection_name))
        if existing_dim is not None and existing_dim != vector_dim:
            raise ValueError(
                f'增量同步失败：Qdrant 现有维度={existing_dim}，本地向量维度={vector_dim}，不一致。'
                f'请用 recreate=True 重建 collection（命令行: sync-qdrant，不加 --no-recreate）。'
            )

    only_set = {str(c) for c in only_case_ids} if only_case_ids is not None else None

    points = []
    for i, row in metadata.iterrows():
        payload: Dict[str, Any] = {}
        for col in metadata.columns:
            if col in _QDRANT_PAYLOAD_EXCLUDE:
                continue
            payload[col] = _qdrant_clean_value(row[col])

        if payload.get('case_id') is None:
            work_order_no = payload.get('work_order_no', 'unknown')
            time_order = payload.get('time_order', i)
            payload['case_id'] = f'{work_order_no}__{time_order}'

        if only_set is not None and str(payload.get('case_id')) not in only_set:
            continue

        points.append(PointStruct(id=int(i), vector=vectors[i].tolist(), payload=payload))

    mode_text = '重建整库' if recreate else (f'增量 {len(points)} 条' if only_set is not None else '全量 upsert')
    print(f'开始同步到 Qdrant（{mode_text}）...')
    if points:
        client.upsert(collection_name=collection_name, points=points)
    else:
        print('没有需要写入的 point（增量集合为空）。')
    info = client.get_collection(collection_name)
    print(f'Qdrant 同步完成: collection={collection_name}, dim={vector_dim}, '
          f'本次写入={len(points)}, 库内总数={info.points_count}')
    return len(points)


# ============================================================
# 11.6 灾备：打包备份 / 一键恢复
# ============================================================


def list_backups(backup_dir: str | Path = DEFAULT_BACKUP_DIR) -> List[Path]:
    """按时间从旧到新列出所有备份 zip。"""
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob(f'{BACKUP_PREFIX}*.zip'))


def _rotate_backups(backup_dir: Path, keep_last: Optional[int]) -> None:
    if keep_last is None or keep_last <= 0:
        return
    backups = list_backups(backup_dir)
    excess = len(backups) - keep_last
    for old in backups[:max(0, excess)]:
        try:
            old.unlink()
            print('删除过期备份:', old)
        except Exception as e:
            print('删除过期备份失败:', old, e)


def backup_case_library(
    library_dir: str | Path = DEFAULT_SAVE_DIR,
    case_csv: str | Path = DEFAULT_FULL_CASE_CSV,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    keep_last: Optional[int] = DEFAULT_BACKUP_KEEP_LAST,
) -> Path:
    """把整个 case_library/ 文件夹 + 全量故障时间 CSV 打成带时间戳的 zip。

    zip 内部结构：
        case_library/...        （vectors.npy / metadata.csv / *.joblib / manifest.json 等）
        <case_csv 文件名>        （case_input_all_times.csv）
        BACKUP_MANIFEST.json     （备份元信息，便于核对）

    备份完成后按 keep_last 滚动删除最旧的备份。返回生成的 zip 路径。
    """
    library_dir = Path(library_dir)
    if not library_dir.exists():
        raise FileNotFoundError(f'找不到案例库目录，无法备份: {library_dir}')

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_path = backup_dir / f'{BACKUP_PREFIX}{ts}.zip'

    case_csv_path = Path(case_csv)
    file_count = 0

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(library_dir.rglob('*')):
            if p.is_file():
                arcname = Path('case_library') / p.relative_to(library_dir)
                zf.write(p, arcname=str(arcname))
                file_count += 1

        if case_csv_path.exists():
            zf.write(case_csv_path, arcname=case_csv_path.name)
            file_count += 1
        else:
            print('提示: 未找到全量故障时间 CSV，跳过:', case_csv_path)

        backup_manifest = {
            'created_at': datetime.now().isoformat(),
            'library_dir': os.path.abspath(str(library_dir)),
            'case_csv': os.path.abspath(str(case_csv_path)),
            'case_csv_included': case_csv_path.exists(),
            'file_count': file_count,
        }
        zf.writestr('BACKUP_MANIFEST.json', json.dumps(backup_manifest, ensure_ascii=False, indent=2))

    size_kb = zip_path.stat().st_size / 1024
    print(f'备份完成: {zip_path}（{file_count} 个文件, {size_kb:.1f} KB）')

    _rotate_backups(backup_dir, keep_last)
    return zip_path


def _resolve_backup_path(backup: Optional[str | Path], backup_dir: str | Path) -> Path:
    backup_dir = Path(backup_dir)
    if backup in (None, '', 'latest'):
        candidates = list_backups(backup_dir)
        if not candidates:
            raise FileNotFoundError(f'{backup_dir} 下没有任何备份（{BACKUP_PREFIX}*.zip）')
        return candidates[-1]

    backup_path = Path(backup)
    if backup_path.exists():
        return backup_path

    alt = backup_dir / str(backup)
    if alt.exists():
        return alt

    raise FileNotFoundError(f'找不到指定备份: {backup}')


def restore_case_library(
    backup: Optional[str | Path] = 'latest',
    library_dir: str | Path = DEFAULT_SAVE_DIR,
    case_csv: str | Path = DEFAULT_FULL_CASE_CSV,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    safety_backup: bool = True,
    sync_qdrant: bool = True,
    qdrant_url: str = DEFAULT_QDRANT_URL,
    qdrant_collection: str = DEFAULT_QDRANT_COLLECTION,
) -> Path:
    """从备份 zip 还原本地案例库，并默认重建 Qdrant。

    backup: 'latest'(默认，取最新一份)、备份 zip 路径、或 backup_dir 下的文件名。
    safety_backup: 覆盖前先把当前案例库再备份一份，避免误恢复后旧状态丢失。
    """
    backup_path = _resolve_backup_path(backup, backup_dir)
    print('使用备份:', backup_path)

    library_dir = Path(library_dir)
    case_csv = Path(case_csv)

    if safety_backup and library_dir.exists():
        print('覆盖前先把当前状态安全备份一份...')
        try:
            backup_case_library(library_dir=library_dir, case_csv=case_csv, backup_dir=backup_dir)
        except Exception as e:
            print('警告: 恢复前的安全备份失败（继续恢复）:', e)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        with zipfile.ZipFile(backup_path) as zf:
            zf.extractall(tmp)

        src_lib = tmp / 'case_library'
        if not src_lib.exists():
            raise ValueError(f'备份内不含 case_library/ 目录，无法恢复: {backup_path}')

        if library_dir.exists():
            shutil.rmtree(library_dir)
        shutil.copytree(src_lib, library_dir)
        print('已恢复案例库目录:', library_dir)

        src_csv = tmp / case_csv.name
        if src_csv.exists():
            case_csv.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_csv, case_csv)
            print('已恢复全量故障时间 CSV:', case_csv)
        else:
            print('提示: 备份内不含 CSV，跳过 CSV 恢复。')

    if sync_qdrant:
        print('\n========== 恢复后重建 Qdrant ==========')
        try:
            sync_library_to_qdrant(
                library_dir=library_dir,
                qdrant_url=qdrant_url,
                collection_name=qdrant_collection,
                recreate=True,
            )
        except Exception as e:
            print('警告: 本地文件已恢复，但重建 Qdrant 失败:', e)
            print('Qdrant 服务恢复后可手动同步:')
            print(f'    python vcb_case_library_direct.py sync-qdrant --library-dir "{library_dir}"')

    print('恢复完成。')
    return backup_path


def build_case_library(
    case_csv: str | Path = DEFAULT_FULL_CASE_CSV,
    desc_csv: Optional[str | Path] = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    save_dir: str | Path = DEFAULT_SAVE_DIR,
    time_is_cst: bool = True,
) -> Tuple[pd.DataFrame, np.ndarray, Dict[str, int]]:
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    work_df = load_case_input_for_build(case_csv, desc_csv=desc_csv)
    print('工单事件数量:', len(work_df))
    print('数据目录:', data_dir)
    print('保存目录:', save_dir)

    ok_records, process_logs, segments_store, node_counts_store, missing_date_rows = process_work_df(work_df, data_dir=data_dir, time_is_cst=time_is_cst)

    if len(ok_records) == 0:
        process_log_df = pd.DataFrame(process_logs)
        process_log_df.to_csv(save_dir / 'process_log.csv', index=False, encoding='utf-8-sig')
        if len(missing_date_rows) > 0:
            pd.DataFrame(missing_date_rows).to_csv(save_dir / DEFAULT_MISSING_DATE_CSV, index=False, encoding='utf-8-sig')
        raise Exception('没有任何工单成功生成向量，请检查时间、车组、01_数据整理路径和 target_column。process_log.csv 已保存。')

    metadata_df = pd.DataFrame(ok_records)
    process_log_df = pd.DataFrame(process_logs)
    missing_date_df = pd.DataFrame(missing_date_rows)

    return save_case_library_files(
        save_dir=save_dir,
        metadata_df=metadata_df,
        process_log_df=process_log_df,
        segments_store=segments_store,
        node_counts_store=node_counts_store,
        data_dir=data_dir,
        case_csv=case_csv,
        time_is_cst=time_is_cst,
        missing_date_df=missing_date_df,
        update_info={'mode': 'build'},
    )


# ============================================================
# 12. 查询新故障
# ============================================================


def query_similar_cases(
    train: Any,
    fault_time: Any,
    library_dir: str | Path = DEFAULT_SAVE_DIR,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    time_is_cst: bool = True,
    top_k: int = 5,
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    library_dir = Path(library_dir)
    vectors = np.load(library_dir / 'vectors.npy')
    metadata = pd.read_csv(library_dir / 'metadata.csv')
    node_index = joblib.load(library_dir / 'node_index.joblib')

    result = get_local_case_result(train=train, fault_time=fault_time, data_dir=data_dir, time_is_cst=time_is_cst)
    if result['status'] != 'ok':
        print('新故障没有成功生成 segment，status =', result['status'])
        return result, pd.DataFrame()

    node_counts = result['node_counts']
    query_vec, unknown_nodes = vectorize_node_counts(node_counts, node_index)
    if np.linalg.norm(query_vec) == 0:
        print('新故障生成的节点都不在历史 node_index 里，无法有效相似度检索。')
        return result, pd.DataFrame()

    scores = vectors @ query_vec
    top_idx = np.argsort(-scores)[:top_k]
    metadata_by_row = metadata.set_index('row_index')

    rows: List[Dict[str, Any]] = []
    for rank, row_idx in enumerate(top_idx, start=1):
        meta = metadata_by_row.loc[int(row_idx)].to_dict()
        rows.append({
            'rank': rank,
            'similarity': float(scores[row_idx]),
            'case_id': meta.get('case_id'),
            'work_order_no': meta.get('work_order_no'),
            'time_order': meta.get('time_order'),
            'train': meta.get('train'),
            'fault_time_raw': meta.get('fault_time_raw'),
            'fault_time_utc': meta.get('fault_time_utc'),
            'segment_start': meta.get('segment_start'),
            'segment_end': meta.get('segment_end'),
            'time_diff_seconds': meta.get('time_diff_seconds'),
            'remark': meta.get('remark'),
            'long_description': meta.get('long_description'),
        })

    result_table = pd.DataFrame(rows)
    print('新故障状态:', result['status'])
    print('新故障 UTC 时间:', result['fault_time_utc'])
    print('新故障 node 数量:', len(node_counts))
    print('未知 node 数量:', len(unknown_nodes))
    return result, result_table


# ============================================================
# 13. work_order_fault_extractor 串联
# ============================================================


def _require_work_order_extractor() -> Dict[str, Any]:
    try:
        from work_order_fault_extractor import (  # type: ignore
            convert_work_orders_xlsx_to_csv,
            _read_xlsx_sheets,
            _find_header_row_index,
            _clean_header,
            _row_to_dict,
            _clean_cell,
            _build_required_column_map,
            _is_blank_work_order_row,
            _extract_events_with_llm,
            _extract_events_with_regex,
            _normalize_fault_time,
            _make_remark,
        )
    except Exception as e:
        raise ImportError(
            '没有成功导入 work_order_fault_extractor.py。请确认该文件和本文件在同一目录，'
            '并且 llm_api_tool.py/config 配置可用。如果不用LLM，可运行时加 --no-llm，但该文件仍需可导入。'
        ) from e

    return locals()


def split_case_csv_by_fault_time(case_csv: str | Path, out_no_time_csv: str | Path = DEFAULT_NO_TIME_CSV) -> Tuple[pd.DataFrame, pd.DataFrame]:
    case_csv = Path(case_csv)
    out_no_time_csv = Path(out_no_time_csv)
    df = pd.read_csv(case_csv, dtype=str, encoding='utf-8-sig')
    df.columns = [str(c).strip() for c in df.columns]

    if 'fault_time' not in df.columns:
        raise ValueError(f'case_csv 中找不到 fault_time 列。当前列: {list(df.columns)}')

    fault_time_text = df['fault_time'].fillna('').astype(str).str.strip()
    no_time_mask = fault_time_text.eq('') | fault_time_text.str.lower().isin(['nan', 'nat', 'none', 'null'])
    df_no_time = df[no_time_mask].copy()
    if len(df_no_time) > 0:
        df_no_time['extract_status'] = 'no_explicit_fault_time'

    out_no_time_csv.parent.mkdir(parents=True, exist_ok=True)
    df_no_time.to_csv(out_no_time_csv, index=False, encoding='utf-8-sig')

    print('case_input_all_times 总行数:', len(df))
    print('有明确 fault_time 行数:', int((~no_time_mask).sum()))
    print('无明确 fault_time 行数:', int(no_time_mask.sum()))
    print('已生成无时间清单:', out_no_time_csv)
    return df, df_no_time


def enrich_case_csv_with_car_no(case_csv: str | Path, work_order_xlsx: str | Path) -> pd.DataFrame:
    case_csv = Path(case_csv)
    rows = read_work_order_rows_from_xlsx(work_order_xlsx)
    car_rows = [
        {
            'work_order_no': safe_str(r.get('work_order_no', '')),
            'car_no': normalize_car_no(r.get('car_no', '')),
        }
        for r in rows
        if safe_str(r.get('work_order_no', '')) != ''
    ]
    car_df = pd.DataFrame(car_rows)
    df = pd.read_csv(case_csv, dtype=str, encoding='utf-8-sig')

    if len(car_df) == 0:
        if 'car_no' not in df.columns:
            df['car_no'] = ''
            df.to_csv(case_csv, index=False, encoding='utf-8-sig')
        return df

    car_df = car_df.drop_duplicates(subset=['work_order_no'], keep='first')
    if 'car_no' in df.columns:
        df = df.drop(columns=['car_no'])
    df = df.merge(car_df, on='work_order_no', how='left')
    df['car_no'] = df['car_no'].fillna('').astype(str).apply(normalize_car_no)

    ordered = [c for c in OUTPUT_COLUMNS if c in df.columns] + [c for c in df.columns if c not in OUTPUT_COLUMNS]
    df = df[ordered]
    df.to_csv(case_csv, index=False, encoding='utf-8-sig')
    print('已回填 CarNO 到 case CSV:', case_csv)
    return df


def extract_case_inputs_from_xlsx(
    work_order_xlsx: str | Path = DEFAULT_WORK_ORDER_XLSX,
    out_case_csv: str | Path = DEFAULT_FULL_CASE_CSV,
    out_no_time_csv: str | Path = DEFAULT_NO_TIME_CSV,
    config_path: Optional[str | Path] = None,
    use_llm: bool = True,
    fallback_to_regex: bool = True,
    print_progress: bool = True,
    progress_every: int = 1,
) -> Dict[str, Any]:
    """
    从原始工单 Excel 提取故障时间，生成 case_input_all_times.csv。

    支持输入：
        1. .xlsx：直接读取；
        2. .xls：先自动转换成 .xlsx，再读取；
        3. 默认工单.xlsx 不存在但同名工单.xls存在：自动转成工单.xlsx。

    输出：
        case_input_all_times.csv
        case_input_no_time.csv
    """
    tools = _require_work_order_extractor()
    convert_work_orders_xlsx_to_csv = tools['convert_work_orders_xlsx_to_csv']

    # ------------------------------------------------------------
    # 1. 先处理 xls -> xlsx
    # ------------------------------------------------------------
    work_order_xlsx = convert_xls_to_xlsx_if_needed(work_order_xlsx)

    out_case_csv = Path(out_case_csv).expanduser().resolve()
    out_no_time_csv = Path(out_no_time_csv).expanduser().resolve()
    config_path = Path(config_path).expanduser().resolve() if config_path else None

    print('\n========== Step 1/2: 从工单 Excel 提取故障时间 ==========')
    print('使用工单文件:', work_order_xlsx)
    print('输出故障时间CSV:', out_case_csv)

    # ------------------------------------------------------------
    # 2. xlsx -> case_input_all_times.csv
    # ------------------------------------------------------------
    summary = convert_work_orders_xlsx_to_csv(
        xlsx_path=work_order_xlsx,
        output_csv_path=out_case_csv,
        config_path=config_path,
        use_llm=use_llm,
        fallback_to_regex=fallback_to_regex,
        print_progress=print_progress,
        progress_every=progress_every,
    )
    enrich_case_csv_with_car_no(out_case_csv, work_order_xlsx)

    # ------------------------------------------------------------
    # 3. 额外输出无明确故障时间清单
    # ------------------------------------------------------------
    print('\n========== Step 2/2: 输出无明确故障时间清单 ==========')

    case_df, no_time_df = split_case_csv_by_fault_time(
        case_csv=out_case_csv,
        out_no_time_csv=out_no_time_csv
    )

    return {
        'extract_summary': summary,
        'work_order_xlsx': str(work_order_xlsx),
        'case_csv': str(out_case_csv),
        'no_time_csv': str(out_no_time_csv),
        'case_rows': int(len(case_df)),
        'no_time_rows': int(len(no_time_df)),
    }


def build_case_library_from_xlsx(
    work_order_xlsx: str | Path = DEFAULT_WORK_ORDER_XLSX,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    save_dir: str | Path = DEFAULT_SAVE_DIR,
    out_case_csv: str | Path = DEFAULT_FULL_CASE_CSV,
    out_no_time_csv: str | Path = DEFAULT_NO_TIME_CSV,
    desc_csv: Optional[str | Path] = None,
    config_path: Optional[str | Path] = None,
    use_llm: bool = True,
    fallback_to_regex: bool = True,
    print_progress: bool = True,
    progress_every: int = 1,
    time_is_cst: bool = True,
) -> Tuple[Dict[str, Any], pd.DataFrame, np.ndarray, Dict[str, int]]:
    input_summary = extract_case_inputs_from_xlsx(
        work_order_xlsx=work_order_xlsx,
        out_case_csv=out_case_csv,
        out_no_time_csv=out_no_time_csv,
        config_path=config_path,
        use_llm=use_llm,
        fallback_to_regex=fallback_to_regex,
        print_progress=print_progress,
        progress_every=progress_every,
    )

    print('\n========== Step 3/3: 读取原始数据，切分 segment 并向量化建库 ==========' )
    metadata_df, vectors, node_index = build_case_library(
        case_csv=input_summary['case_csv'],
        desc_csv=desc_csv,
        data_dir=data_dir,
        save_dir=save_dir,
        time_is_cst=time_is_cst,
    )
    return input_summary, metadata_df, vectors, node_index


# ============================================================
# 14. 增量更新：先按 process_log.csv 筛新增工单，再只解析新增工单
# ============================================================


def get_processed_work_order_set(library_dir: str | Path = DEFAULT_SAVE_DIR) -> set[str]:
    process_log_path = Path(library_dir) / 'process_log.csv'
    if not process_log_path.exists():
        return set()
    df = pd.read_csv(process_log_path, dtype=str, encoding='utf-8-sig')
    if 'work_order_no' not in df.columns:
        return set()
    return set(df['work_order_no'].dropna().astype(str).str.strip())


def read_work_order_rows_from_xlsx(work_order_xlsx: str | Path) -> List[Dict[str, Any]]:
    """
    轻量读取工单 Excel 的完整行，用于增量更新前筛选新增工单。

    支持：
        1. .xlsx：直接读取；
        2. .xls：先自动转换为 .xlsx；
        3. 默认 工单.xlsx 不存在但 工单.xls 存在：自动转换。
    """
    work_order_xlsx = convert_xls_to_xlsx_if_needed(work_order_xlsx)

    tools = _require_work_order_extractor()
    _read_xlsx_sheets = tools['_read_xlsx_sheets']
    _find_header_row_index = tools['_find_header_row_index']
    _clean_header = tools['_clean_header']
    _row_to_dict = tools['_row_to_dict']
    _clean_cell = tools['_clean_cell']
    _build_required_column_map = tools['_build_required_column_map']
    _is_blank_work_order_row = tools['_is_blank_work_order_row']

    sheets = _read_xlsx_sheets(Path(work_order_xlsx))
    output_rows: List[Dict[str, Any]] = []

    for sheet_name, rows in sheets:
        header_index = _find_header_row_index(rows)
        if header_index is None:
            continue

        headers = [_clean_header(x) for x in rows[header_index]]
        required = _build_required_column_map(headers)

        for raw_row in rows[header_index + 1:]:
            row = _row_to_dict(headers, raw_row)

            if _is_blank_work_order_row(row, required):
                continue

            output_rows.append({
                'sheet_name': sheet_name,
                'work_order_no': _clean_cell(row.get(required['work_order_no'], '')),
                'train': _clean_cell(row.get(required['train'], '')),
                'car_no': normalize_car_no(row.get('CarNO.', row.get('CarNO', row.get('car_no', '')))),
                'description': _clean_cell(row.get(required['description'], '')),
                'report_date': _clean_cell(row.get('Report Date', '')),
                'change_date': _clean_cell(row.get('Change Date', '')),
            })

    return output_rows


def extract_case_inputs_from_rows(
    rows: Sequence[Dict[str, Any]],
    out_case_csv: str | Path = DEFAULT_UPDATE_CASE_CSV,
    out_no_time_csv: str | Path = DEFAULT_UPDATE_NO_TIME_CSV,
    config_path: Optional[str | Path] = None,
    use_llm: bool = True,
    fallback_to_regex: bool = True,
    print_progress: bool = True,
    progress_every: int = 1,
) -> pd.DataFrame:
    tools = _require_work_order_extractor()
    _extract_events_with_llm = tools['_extract_events_with_llm']
    _extract_events_with_regex = tools['_extract_events_with_regex']
    _normalize_fault_time = tools['_normalize_fault_time']
    _make_remark = tools['_make_remark']

    output_rows: List[Dict[str, Any]] = []
    progress_every = max(1, int(progress_every or 1))

    for i, row in enumerate(rows, start=1):
        work_order_no = safe_str(row.get('work_order_no', ''))
        train = normalize_train(row.get('train', ''))
        car_no = normalize_car_no(row.get('car_no', ''))
        description = safe_str(row.get('description', ''))
        report_date = safe_str(row.get('report_date', ''))
        change_date = safe_str(row.get('change_date', ''))

        if print_progress and (i == 1 or i % progress_every == 0):
            print(f'[update extractor] 解析新增工单 {i}/{len(rows)}: work_order_no={work_order_no}, train={train}', flush=True)

        events: List[Dict[str, str]] = []
        if use_llm and description:
            try:
                events = _extract_events_with_llm(
                    description=description,
                    report_date=report_date,
                    change_date=change_date,
                    config_path=Path(config_path) if config_path else None,
                )
            except Exception:
                events = []

        if (not events) and fallback_to_regex:
            try:
                events = _extract_events_with_regex(description, report_date or change_date)
            except Exception:
                events = []

        if not events:
            events = [{'fault_time': '', 'remark': _make_remark(description, 0)}]

        for time_order, event in enumerate(events):
            fault_time = _normalize_fault_time(str(event.get('fault_time', '')))
            remark = safe_str(event.get('remark', '')) or _make_remark(description, time_order)
            output_rows.append({
                'work_order_no': work_order_no,
                'train': train,
                'car_no': car_no,
                'fault_time': fault_time,
                'time_order': str(time_order),
                'remark': remark,
            })

    df_update = pd.DataFrame(output_rows, columns=OUTPUT_COLUMNS)
    Path(out_case_csv).parent.mkdir(parents=True, exist_ok=True)
    df_update.to_csv(out_case_csv, index=False, encoding='utf-8-sig')
    print('已生成本次新增故障时间表:', out_case_csv, '行数:', len(df_update))

    split_case_csv_by_fault_time(out_case_csv, out_no_time_csv)
    return df_update


def append_update_case_to_full_case_csv(update_case_csv: str | Path, full_case_csv: str | Path = DEFAULT_FULL_CASE_CSV) -> pd.DataFrame:
    update_df = pd.read_csv(update_case_csv, dtype=str, encoding='utf-8-sig')
    full_case_csv = Path(full_case_csv)

    if full_case_csv.exists():
        full_df = pd.read_csv(full_case_csv, dtype=str, encoding='utf-8-sig')
        merged = pd.concat([full_df, update_df], ignore_index=True)
    else:
        merged = update_df.copy()

    # 稳妥去重；正常业务只新增新工单，因此不会误删有效新行。
    key_cols = [c for c in ['work_order_no', 'fault_time', 'time_order'] if c in merged.columns]
    if key_cols:
        merged = merged.drop_duplicates(subset=key_cols, keep='first')
    else:
        merged = merged.drop_duplicates(keep='first')

    merged.to_csv(full_case_csv, index=False, encoding='utf-8-sig')
    print('已维护全量故障时间表:', full_case_csv, '总行数:', len(merged))
    return merged


def update_case_library_from_xlsx(
    work_order_xlsx: str | Path = DEFAULT_WORK_ORDER_XLSX,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    library_dir: str | Path = DEFAULT_SAVE_DIR,
    full_case_csv: str | Path = DEFAULT_FULL_CASE_CSV,
    update_case_csv: str | Path = DEFAULT_UPDATE_CASE_CSV,
    update_no_time_csv: str | Path = DEFAULT_UPDATE_NO_TIME_CSV,
    config_path: Optional[str | Path] = None,
    use_llm: bool = True,
    fallback_to_regex: bool = True,
    print_progress: bool = True,
    progress_every: int = 1,
    time_is_cst: bool = True,
    retry_failed_work_orders: bool = False,
    use_gateway_download: bool = True,
    gateway_output_root: str | Path = DEFAULT_GATEWAY_WORK_ORDER_ROOT,
    gateway_endpoint: Optional[str] = None,
    gateway_metrics_json: Optional[str] = None,
    gateway_batch_size: int = 25,
    gateway_fallback_to_local: bool = True,
    sync_qdrant: bool = True,
    qdrant_url: str = DEFAULT_QDRANT_URL,
    qdrant_collection: str = DEFAULT_QDRANT_COLLECTION,
    qdrant_force_recreate: bool = False,
    backup: bool = False,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    backup_keep_last: Optional[int] = DEFAULT_BACKUP_KEEP_LAST,
) -> Tuple[pd.DataFrame, np.ndarray, Dict[str, int]]:
    library_dir = Path(library_dir)
    library_dir.mkdir(parents=True, exist_ok=True)

    # 支持 update-from-xlsx 时输入 .xls，或默认 工单.xlsx 不存在但 工单.xls 存在
    work_order_xlsx = convert_xls_to_xlsx_if_needed(work_order_xlsx)

    processed_set = get_processed_work_order_set(library_dir)

    if retry_failed_work_orders and (library_dir / 'process_log.csv').exists():
        log_df = pd.read_csv(library_dir / 'process_log.csv', dtype=str, encoding='utf-8-sig')
        ok_set = set(log_df.loc[log_df['status'] == 'ok', 'work_order_no'].dropna().astype(str).str.strip())
        processed_set = ok_set

    all_rows = read_work_order_rows_from_xlsx(work_order_xlsx)
    new_rows = [r for r in all_rows if safe_str(r.get('work_order_no', '')) not in processed_set]

    print('新 Excel 工单行数:', len(all_rows))
    print('旧库已处理工单号数量:', len(processed_set))
    print('本次新增工单行数:', len(new_rows))

    if len(new_rows) == 0:
        print('本次没有新增工单，案例库不更新。')
        # 返回现有库
        metadata = pd.read_csv(library_dir / 'metadata.csv') if (library_dir / 'metadata.csv').exists() else pd.DataFrame()
        vectors = np.load(library_dir / 'vectors.npy') if (library_dir / 'vectors.npy').exists() else np.zeros((0, 0))
        node_index = joblib.load(library_dir / 'node_index.joblib') if (library_dir / 'node_index.joblib').exists() else {}
        return metadata, vectors, node_index

    df_update = extract_case_inputs_from_rows(
        rows=new_rows,
        out_case_csv=update_case_csv,
        out_no_time_csv=update_no_time_csv,
        config_path=config_path,
        use_llm=use_llm,
        fallback_to_regex=fallback_to_regex,
        print_progress=print_progress,
        progress_every=progress_every,
    )

    append_update_case_to_full_case_csv(update_case_csv, full_case_csv=full_case_csv)

    update_work_df = load_case_input_for_build(update_case_csv, desc_csv=None)
    if use_gateway_download:
        print('\n========== 从网关下载真实工单故障窗口并向量化 ==========')
        ok_records_new, process_logs_new, segments_new, node_counts_new, missing_date_rows_new = process_work_df_with_gateway(
            update_work_df,
            data_dir=data_dir,
            time_is_cst=time_is_cst,
            gateway_output_root=gateway_output_root,
            gateway_endpoint=gateway_endpoint,
            gateway_metrics_json=gateway_metrics_json,
            gateway_batch_size=gateway_batch_size,
            fallback_to_local_data=gateway_fallback_to_local,
        )
    else:
        print('\n========== 使用 01_数据整理 处理真实工单（已关闭网关下载） ==========')
        ok_records_new, process_logs_new, segments_new, node_counts_new, missing_date_rows_new = process_work_df(
            update_work_df, data_dir=data_dir, time_is_cst=time_is_cst
        )

    # 读取旧库
    if (library_dir / 'metadata.csv').exists():
        metadata_old = pd.read_csv(library_dir / 'metadata.csv')
    else:
        metadata_old = pd.DataFrame()

    if (library_dir / 'process_log.csv').exists():
        process_log_old = pd.read_csv(library_dir / 'process_log.csv')
    else:
        process_log_old = pd.DataFrame()

    if (library_dir / 'segments.joblib').exists():
        segments_store = joblib.load(library_dir / 'segments.joblib')
    else:
        segments_store = {}

    if (library_dir / 'node_counts.joblib').exists():
        node_counts_store = joblib.load(library_dir / 'node_counts.joblib')
    else:
        node_counts_store = {}

    # 记录更新前的 node_index，用于判断维度是否变化（决定 Qdrant 增量 or 重建）
    if (library_dir / 'node_index.joblib').exists():
        old_node_index = joblib.load(library_dir / 'node_index.joblib')
    else:
        old_node_index = {}

    metadata_new = pd.DataFrame(ok_records_new)
    process_log_new = pd.DataFrame(process_logs_new)

    metadata_full = pd.concat([metadata_old, metadata_new], ignore_index=True) if len(metadata_old) > 0 else metadata_new
    process_log_full = pd.concat([process_log_old, process_log_new], ignore_index=True) if len(process_log_old) > 0 else process_log_new

    # 避免重复追加
    if len(metadata_full) > 0 and 'case_id' in metadata_full.columns:
        metadata_full = metadata_full.drop_duplicates(subset=['case_id'], keep='first').reset_index(drop=True)
    if len(process_log_full) > 0:
        dedup_cols = [c for c in ['work_order_no', 'fault_time_raw', 'time_order'] if c in process_log_full.columns]
        if dedup_cols:
            process_log_full = process_log_full.drop_duplicates(subset=dedup_cols, keep='first').reset_index(drop=True)

    segments_store.update(segments_new)
    node_counts_store.update(node_counts_new)

    # missing_date_csv 追加维护
    missing_path = library_dir / DEFAULT_MISSING_DATE_CSV
    missing_old = pd.read_csv(missing_path, dtype=str, encoding='utf-8-sig') if missing_path.exists() else pd.DataFrame()
    missing_new = pd.DataFrame(missing_date_rows_new)
    missing_full = pd.concat([missing_old, missing_new], ignore_index=True) if len(missing_old) > 0 else missing_new
    if len(missing_full) > 0:
        dedup_cols = [c for c in ['work_order_no', 'fault_time_raw', 'time_order'] if c in missing_full.columns]
        if dedup_cols:
            missing_full = missing_full.drop_duplicates(subset=dedup_cols, keep='first').reset_index(drop=True)

    if len(metadata_full) == 0:
        process_log_full.to_csv(library_dir / 'process_log.csv', index=False, encoding='utf-8-sig')
        if len(missing_full) > 0:
            missing_full.to_csv(missing_path, index=False, encoding='utf-8-sig')
        raise Exception('本次更新后仍没有任何成功案例。process_log.csv 已更新。')

    metadata_saved, vectors_saved, node_index_saved = save_case_library_files(
        save_dir=library_dir,
        metadata_df=metadata_full,
        process_log_df=process_log_full,
        segments_store=segments_store,
        node_counts_store=node_counts_store,
        data_dir=data_dir,
        case_csv=full_case_csv,
        time_is_cst=time_is_cst,
        missing_date_df=missing_full,
        update_info={
            'mode': 'update-from-xlsx',
            'source_xlsx': str(work_order_xlsx),
            'new_work_order_rows': len(new_rows),
            'update_case_rows': len(df_update),
            'new_ok_count': len(ok_records_new),
            'new_log_count': len(process_logs_new),
            'gateway_download': bool(use_gateway_download),
            'gateway_output_root': str(gateway_output_root) if use_gateway_download else '',
            'gateway_fallback_to_local': bool(gateway_fallback_to_local),
        },
    )

    if sync_qdrant:
        print('\n========== 同步更新后的案例库到 Qdrant ==========')

        # 自动判断：node_index（节点集合与顺序）没变 -> 维度没变、老向量没变 -> 只增量 upsert 新工单；
        # 变了 -> 老向量全部失效，必须删表重建。第一次建库（旧 index 为空）也走重建。
        dim_changed = list(old_node_index.keys()) != list(node_index_saved.keys())
        do_recreate = qdrant_force_recreate or dim_changed or len(old_node_index) == 0
        new_case_ids = set(str(c) for c in node_counts_new.keys())

        if do_recreate:
            if qdrant_force_recreate:
                reason = '指定了强制重建'
            elif len(old_node_index) == 0:
                reason = '首次同步/旧库无 node_index'
            else:
                reason = f'出现新节点，向量维度变化 {len(old_node_index)} -> {len(node_index_saved)}'
            print(f'同步方式：删表重建（原因：{reason}）')
        else:
            print(f'同步方式：增量 upsert，仅写入本次新增的 {len(new_case_ids)} 个 case（维度未变 {len(node_index_saved)}）')

        try:
            sync_library_to_qdrant(
                library_dir=library_dir,
                qdrant_url=qdrant_url,
                collection_name=qdrant_collection,
                recreate=do_recreate,
                only_case_ids=None if do_recreate else new_case_ids,
            )
        except Exception as e:
            print('警告: 本地案例库已更新成功，但同步到 Qdrant 失败:', e)
            print('Qdrant 服务恢复后可手动同步:')
            print(f'    python vcb_case_library_direct.py sync-qdrant --library-dir "{library_dir}"')
    else:
        print('已跳过 Qdrant 同步（--no-qdrant）。如需查询，请稍后手动同步:')
        print(f'    python vcb_case_library_direct.py sync-qdrant --library-dir "{library_dir}"')

    if backup:
        print('\n========== 更新成功，自动备份案例库 ==========')
        try:
            backup_case_library(
                library_dir=library_dir,
                case_csv=full_case_csv,
                backup_dir=backup_dir,
                keep_last=backup_keep_last,
            )
        except Exception as e:
            print('警告: 案例库更新成功，但自动备份失败:', e)

    return metadata_saved, vectors_saved, node_index_saved


# ============================================================
# 15. 命令行入口
# ============================================================


def main(argv: Optional[Sequence[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    # 默认直接执行 extract-build
    if len(argv) == 0:
        argv = ['extract-build']

    parser = argparse.ArgumentParser(description='VCB/1757 工单Excel -> 故障时间CSV -> segment向量案例库')
    subparsers = parser.add_subparsers(dest='mode', required=True)

    extract_parser = subparsers.add_parser('extract', help='从原始工单 xlsx 生成 case_input_all_times.csv')
    extract_parser.add_argument('--work-order-xlsx', default=DEFAULT_WORK_ORDER_XLSX)
    extract_parser.add_argument('--out-case-csv', default=DEFAULT_FULL_CASE_CSV)
    extract_parser.add_argument('--out-no-time-csv', default=DEFAULT_NO_TIME_CSV)
    extract_parser.add_argument('--config', default=None)
    extract_parser.add_argument('--no-llm', action='store_true')
    extract_parser.add_argument('--no-fallback', action='store_true')
    extract_parser.add_argument('--quiet', action='store_true')
    extract_parser.add_argument('--progress-every', type=int, default=1)

    eb_parser = subparsers.add_parser('extract-build', help='一键：原始工单 xlsx -> case_input_all_times.csv -> 案例库')
    eb_parser.add_argument('--work-order-xlsx', default=DEFAULT_WORK_ORDER_XLSX)
    eb_parser.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    eb_parser.add_argument('--save-dir', default=DEFAULT_SAVE_DIR)
    eb_parser.add_argument('--out-case-csv', default=DEFAULT_FULL_CASE_CSV)
    eb_parser.add_argument('--out-no-time-csv', default=DEFAULT_NO_TIME_CSV)
    eb_parser.add_argument('--desc-csv', default=None)
    eb_parser.add_argument('--config', default=None)
    eb_parser.add_argument('--no-llm', action='store_true')
    eb_parser.add_argument('--no-fallback', action='store_true')
    eb_parser.add_argument('--quiet', action='store_true')
    eb_parser.add_argument('--progress-every', type=int, default=1)
    eb_parser.add_argument('--time-is-utc', action='store_true', help='如果 fault_time 已经是 UTC，才加这个参数；默认按 CST 减 8 小时')

    build_parser = subparsers.add_parser('build', help='已有 case_input_all_times.csv 时，直接建库')
    build_parser.add_argument('--case-csv', default=DEFAULT_FULL_CASE_CSV)
    build_parser.add_argument('--desc-csv', default=None)
    build_parser.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    build_parser.add_argument('--save-dir', default=DEFAULT_SAVE_DIR)
    build_parser.add_argument('--time-is-utc', action='store_true')

    update_parser = subparsers.add_parser('update-from-xlsx', help='增量更新：只解析并处理新增工单')
    update_parser.add_argument('--work-order-xlsx', default=DEFAULT_WORK_ORDER_XLSX)
    update_parser.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    update_parser.add_argument('--library-dir', default=DEFAULT_SAVE_DIR)
    update_parser.add_argument('--full-case-csv', default=DEFAULT_FULL_CASE_CSV)
    update_parser.add_argument('--update-case-csv', default=DEFAULT_UPDATE_CASE_CSV)
    update_parser.add_argument('--update-no-time-csv', default=DEFAULT_UPDATE_NO_TIME_CSV)
    update_parser.add_argument('--config', default=None)
    update_parser.add_argument('--no-llm', action='store_true')
    update_parser.add_argument('--no-fallback', action='store_true')
    update_parser.add_argument('--quiet', action='store_true')
    update_parser.add_argument('--progress-every', type=int, default=1)
    update_parser.add_argument('--time-is-utc', action='store_true')
    update_parser.add_argument('--retry-failed-work-orders', action='store_true')
    update_parser.add_argument('--no-gateway-download', action='store_true', help='不从网关下载新增真实工单，沿用 01_数据整理')
    update_parser.add_argument('--gateway-output-root', default=DEFAULT_GATEWAY_WORK_ORDER_ROOT)
    update_parser.add_argument('--gateway-endpoint', default=None, help='网关接口；默认读取 RAILIVERSE_ENDPOINT')
    update_parser.add_argument('--gateway-metrics-json', default=None)
    update_parser.add_argument('--gateway-batch-size', type=int, default=25)
    update_parser.add_argument('--no-gateway-local-fallback', action='store_true', help='网关失败时不再尝试 01_数据整理')
    update_parser.add_argument('--no-qdrant', action='store_true', help='更新后不自动同步到 Qdrant')
    update_parser.add_argument('--qdrant-url', default=DEFAULT_QDRANT_URL)
    update_parser.add_argument('--qdrant-collection', default=DEFAULT_QDRANT_COLLECTION)
    update_parser.add_argument('--qdrant-force-recreate', action='store_true', help='强制删表重建（默认自动判断：维度没变只增量 upsert）')
    update_parser.add_argument('--backup', action='store_true', help='更新成功后自动打包备份案例库')
    update_parser.add_argument('--backup-dir', default=DEFAULT_BACKUP_DIR)
    update_parser.add_argument('--backup-keep-last', type=int, default=DEFAULT_BACKUP_KEEP_LAST)

    sync_parser = subparsers.add_parser('sync-qdrant', help='把本地案例库同步进 Qdrant')
    sync_parser.add_argument('--library-dir', default=DEFAULT_SAVE_DIR)
    sync_parser.add_argument('--qdrant-url', default=DEFAULT_QDRANT_URL)
    sync_parser.add_argument('--qdrant-collection', default=DEFAULT_QDRANT_COLLECTION)
    sync_parser.add_argument('--no-recreate', action='store_true', help='不删表重建，仅 upsert（向量维度变化时会失败）')

    backup_parser = subparsers.add_parser('backup', help='打包备份案例库（case_library/ + 全量CSV）')
    backup_parser.add_argument('--library-dir', default=DEFAULT_SAVE_DIR)
    backup_parser.add_argument('--case-csv', default=DEFAULT_FULL_CASE_CSV)
    backup_parser.add_argument('--backup-dir', default=DEFAULT_BACKUP_DIR)
    backup_parser.add_argument('--keep-last', type=int, default=DEFAULT_BACKUP_KEEP_LAST, help='保留最近 N 份，0 表示不删')

    restore_parser = subparsers.add_parser('restore', help='从备份恢复案例库并重建 Qdrant')
    restore_parser.add_argument('--backup', default='latest', help="'latest'(默认)、备份zip路径、或 backup-dir 下的文件名")
    restore_parser.add_argument('--library-dir', default=DEFAULT_SAVE_DIR)
    restore_parser.add_argument('--case-csv', default=DEFAULT_FULL_CASE_CSV)
    restore_parser.add_argument('--backup-dir', default=DEFAULT_BACKUP_DIR)
    restore_parser.add_argument('--no-safety-backup', action='store_true', help='恢复前不再给当前状态做安全备份')
    restore_parser.add_argument('--no-qdrant', action='store_true', help='恢复后不重建 Qdrant')
    restore_parser.add_argument('--qdrant-url', default=DEFAULT_QDRANT_URL)
    restore_parser.add_argument('--qdrant-collection', default=DEFAULT_QDRANT_COLLECTION)

    list_backups_parser = subparsers.add_parser('list-backups', help='列出已有备份')
    list_backups_parser.add_argument('--backup-dir', default=DEFAULT_BACKUP_DIR)

    query_parser = subparsers.add_parser('query', help='查询新故障相似工单')
    query_parser.add_argument('--train', required=True)
    query_parser.add_argument('--fault-time', required=True)
    query_parser.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    query_parser.add_argument('--library-dir', default=DEFAULT_SAVE_DIR)
    query_parser.add_argument('--top-k', type=int, default=5)
    query_parser.add_argument('--time-is-utc', action='store_true')

    args = parser.parse_args(list(argv))

    if args.mode == 'extract':
        summary = extract_case_inputs_from_xlsx(
            work_order_xlsx=args.work_order_xlsx,
            out_case_csv=args.out_case_csv,
            out_no_time_csv=args.out_no_time_csv,
            config_path=args.config,
            use_llm=not args.no_llm,
            fallback_to_regex=not args.no_fallback,
            print_progress=not args.quiet,
            progress_every=args.progress_every,
        )
        print('\n提取完成:')
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    elif args.mode == 'extract-build':
        build_case_library_from_xlsx(
            work_order_xlsx=args.work_order_xlsx,
            data_dir=args.data_dir,
            save_dir=args.save_dir,
            out_case_csv=args.out_case_csv,
            out_no_time_csv=args.out_no_time_csv,
            desc_csv=args.desc_csv,
            config_path=args.config,
            use_llm=not args.no_llm,
            fallback_to_regex=not args.no_fallback,
            print_progress=not args.quiet,
            progress_every=args.progress_every,
            time_is_cst=not args.time_is_utc,
        )

    elif args.mode == 'build':
        build_case_library(
            case_csv=args.case_csv,
            desc_csv=args.desc_csv,
            data_dir=args.data_dir,
            save_dir=args.save_dir,
            time_is_cst=not args.time_is_utc,
        )

    elif args.mode == 'update-from-xlsx':
        update_case_library_from_xlsx(
            work_order_xlsx=args.work_order_xlsx,
            data_dir=args.data_dir,
            library_dir=args.library_dir,
            full_case_csv=args.full_case_csv,
            update_case_csv=args.update_case_csv,
            update_no_time_csv=args.update_no_time_csv,
            config_path=args.config,
            use_llm=not args.no_llm,
            fallback_to_regex=not args.no_fallback,
            print_progress=not args.quiet,
            progress_every=args.progress_every,
            time_is_cst=not args.time_is_utc,
            retry_failed_work_orders=args.retry_failed_work_orders,
            use_gateway_download=not args.no_gateway_download,
            gateway_output_root=args.gateway_output_root,
            gateway_endpoint=args.gateway_endpoint,
            gateway_metrics_json=args.gateway_metrics_json,
            gateway_batch_size=args.gateway_batch_size,
            gateway_fallback_to_local=not args.no_gateway_local_fallback,
            sync_qdrant=not args.no_qdrant,
            qdrant_url=args.qdrant_url,
            qdrant_collection=args.qdrant_collection,
            qdrant_force_recreate=args.qdrant_force_recreate,
            backup=args.backup,
            backup_dir=args.backup_dir,
            backup_keep_last=args.backup_keep_last,
        )

    elif args.mode == 'sync-qdrant':
        sync_library_to_qdrant(
            library_dir=args.library_dir,
            qdrant_url=args.qdrant_url,
            collection_name=args.qdrant_collection,
            recreate=not args.no_recreate,
        )

    elif args.mode == 'backup':
        backup_case_library(
            library_dir=args.library_dir,
            case_csv=args.case_csv,
            backup_dir=args.backup_dir,
            keep_last=args.keep_last,
        )

    elif args.mode == 'restore':
        restore_case_library(
            backup=args.backup,
            library_dir=args.library_dir,
            case_csv=args.case_csv,
            backup_dir=args.backup_dir,
            safety_backup=not args.no_safety_backup,
            sync_qdrant=not args.no_qdrant,
            qdrant_url=args.qdrant_url,
            qdrant_collection=args.qdrant_collection,
        )

    elif args.mode == 'list-backups':
        backups = list_backups(args.backup_dir)
        if not backups:
            print('没有找到任何备份:', os.path.abspath(args.backup_dir))
        else:
            print(f'共 {len(backups)} 份备份（{os.path.abspath(args.backup_dir)}）:')
            for p in backups:
                size_kb = p.stat().st_size / 1024
                mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                print(f'  {p.name}  {size_kb:8.1f} KB  {mtime}')

    elif args.mode == 'query':
        result, table = query_similar_cases(
            train=args.train,
            fault_time=args.fault_time,
            library_dir=args.library_dir,
            data_dir=args.data_dir,
            time_is_cst=not args.time_is_utc,
            top_k=args.top_k,
        )
        print('\n相似工单结果:')
        print(table.to_string(index=False) if len(table) > 0 else table)


if __name__ == '__main__':
    main()
