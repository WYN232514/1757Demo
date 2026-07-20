# -*- coding: UTF-8 -*-
import requests
import datetime
import os
import pandas as pd
from dotenv import load_dotenv
from gateway_download import parse_gateway_csv

load_dotenv()

def download(
    trainsets=("T1","T2"),
    from_time=None,
    to_time=None,
    out_root="data",
    metrics=None
):
    TRAIN_TYPE = "Rotem"
    if metrics is None:
        raise Exception(
            '请输入待下载内容'
        )
    now = datetime.datetime.now()
    # 默认 昨天 00:00:00 到 今天 00:00:00
    if to_time is None:
        to_time = (now + datetime.timedelta(days=-1)).strftime("%Y-%m-%d 23:59:59")
    if from_time is None:
        from_time = (now + datetime.timedelta(days=-1)).strftime("%Y-%m-%d 00:00:00")
    os.makedirs(out_root, exist_ok=True)

    url = os.getenv("RAILIVERSE_ENDPOINT")
    headers = {
        'Content-Type': 'application/json;charset=UTF-8',
        'Connection': 'close',
        'X-Client-Id': os.getenv('RAILIVERSE_CLIENT_ID'),
        'X-Client-Secret': os.getenv('RAILIVERSE_CLIENT_SECRET'),
    }
    for bucket in trainsets:
        chezu = str(bucket)
        # 为当前车组创建 data/车型/车组 目录
        group_dir = os.path.join(out_root, chezu)
        os.makedirs(group_dir, exist_ok=True)
        payload = {
            "equipModel": TRAIN_TYPE,
            "equipInstance": chezu,
            "startTime": from_time,
            "endTime": to_time,
            "metrics": metrics,
            "paramType": "METRIC",
        }
        print(payload)
        res = requests.post(url, json=payload, headers=headers)
        if not res.ok:
            print(f"HTTP {res.status_code}: {res.text}")
        res.raise_for_status()
        df = parse_gateway_csv(res.content, metrics, chezu)
        date_str = from_time.split(" ")[0].replace("-", "")  # 例如 20251102
        file_name = f"{TRAIN_TYPE}_{chezu}_{date_str}.csv"
        save_path = os.path.join(group_dir, file_name)
        df.to_csv(save_path, index=False, encoding="utf-8")
        print("已保存：", save_path)

date_list=pd.date_range('2026-07-01','2026-07-03')
metrics = [{'code': '910_2_1_02_sd', 'equipUnit': '2'},
{'code': '942_2_1_02_sd', 'equipUnit': '2'},
{'code': '910_2_1_05_sd', 'equipUnit': '5'},
{'code': '942_2_1_05_sd', 'equipUnit': '5'},
{'code': '942_2_1_09_sd', 'equipUnit': '9'},
{'code': '910_2_1_09_sd', 'equipUnit': '9'},
{'code': '943_5_1_02_sd', 'equipUnit': '2'},
{'code': '911_5_1_02_sd', 'equipUnit': '2'},
{'code': '943_5_1_05_sd', 'equipUnit': '5'},
{'code': '911_5_1_05_sd', 'equipUnit': '5'}]
trains_selected=("T1","T2","T3","T4","T5","T6","T7","T8","T9","T10","T11","T12","T13","T14","T15","T16","T17","T18","T19","T20","T21","T22","T23","T24","T25")
for d_map in metrics:
    out_root='dir_' + d_map['code']
    for day in date_list:
        download(trainsets=trains_selected,from_time=str(day).split(' ')[0]+ ' 00:00:00',to_time=str(day).split(' ')[0]+ ' 23:59:59',out_root=out_root,metrics=[d_map])
