import os
import time
from datetime import datetime, timedelta

from iceberg_raw_reader import get_iceberg_table


def main():
    # 使用 IAM Role、标准 AWS 环境变量或本机 config.yaml 连接 Iceberg。
    # 代码中不再保存云访问密钥。
    table = get_iceberg_table()
    table.refresh()
    print(table)

    # 保存目录
    save_dir = "01_数据整理"
    os.makedirs(save_dir, exist_ok=True)

    # 起始日期和结束日期
    start_date = datetime.strptime("2026-07-05", "%Y-%m-%d")
    end_date = datetime.strptime("2026-07-15", "%Y-%m-%d")


    # 记录失败日期
    failed_days = []

    current_date = start_date
    while current_date <= end_date:
        day_str = current_date.strftime("%Y-%m-%d")
        print(f"\n开始下载 {day_str}")

        save_path = os.path.join(save_dir, f"{day_str}.csv")

        # 已下载则跳过
        if os.path.exists(save_path):
            print(f"{day_str} 已存在，跳过")
            current_date += timedelta(days=1)
            continue

        success = False

        for i in range(2):  # 最多尝试2次
            try:
                print(f"第 {i+1} 次尝试")
                a = datetime.now()

                # df = table.scan(row_filter=f"ts_date = '{day_str}'").to_pandas()
                df = table.scan(row_filter=f"ts_date = '{day_str}'").to_pandas()

                print(f"数据加载耗时: {datetime.now() - a}")
                print("数据条数：", len(df))

                if len(df) > 0:
                    df.to_csv(save_path, index=False, encoding="utf-8-sig")
                    print(f"已保存: {save_path}")
                else:
                    print(f"{day_str} 无数据")

                success = True
                break

            except Exception as e:
                print(f"{day_str} 第 {i+1} 次失败: {e}")
                if i == 0:
                    print("3秒后重试一次...")
                    time.sleep(3)

        if not success:
            failed_days.append(day_str)
            print(f"{day_str} 最终失败，已跳过")

        current_date += timedelta(days=1)

    # 保存失败日期
    failed_path = os.path.join(save_dir, "failed_days.txt")
    with open(failed_path, "w", encoding="utf-8") as f:
        for d in failed_days:
            f.write(d + "\n")

    print("\n下载结束")
    print("失败日期：", failed_days)
    print(f"失败日期已保存到: {failed_path}")


if __name__ == "__main__":
    main()
