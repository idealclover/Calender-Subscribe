import os
import csv
from datetime import datetime
from icalendar import Calendar, Event
import pandas as pd
import caldav
from caldav.elements import dav
import uuid

# 添加腾讯云COS SDK
from qcloud_cos import CosConfig
from qcloud_cos import CosS3Client

# 添加腾讯云CDN SDK
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.cdn.v20180606 import cdn_client, models

# 导入配置文件
from config import (
    COS_SECRET_ID,
    COS_SECRET_KEY,
    COS_REGION,
    COS_BUCKET,
    COS_PATH,
    CALDAV_UPLOAD_URL,
    CALDAV_CREDENTIALS,
    CDN_URL,
)


def convert_csv_to_ics(input_folder, output_folder):
    """
    将指定文件夹中的所有CSV文件转换为ICS文件。
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    for filename in os.listdir(input_folder):
        if filename.endswith(".csv"):
            csv_filepath = os.path.join(input_folder, filename)
            ics_filename = os.path.splitext(filename)[0] + ".ics"
            ics_filepath = os.path.join(output_folder, ics_filename)

            # 从文件名提取班级信息（如2025p1.csv -> p1）
            class_name = os.path.splitext(filename)[0]  # 获取不带扩展名的文件名
            calendar_name = class_name  # 直接使用文件名作为日历名称，如"2025p1"

            cal = Calendar()
            cal.add("prodid", "-//My Calendar Events//mxm.dk//")
            cal.add("version", "2.0")
            cal.add("x-wr-calname", calendar_name)  # 设置日历名称
            cal.add("x-wr-timezone", "Asia/Shanghai")  # 设置日历时区

            try:
                df = pd.read_csv(csv_filepath)
            except Exception as e:
                print(
                    f"Error reading {csv_filepath} with pandas: {e}. Trying with csv module."
                )
                with open(csv_filepath, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    data = list(reader)
                df = pd.DataFrame(data[1:], columns=data[0])

            for index, row in df.iterrows():
                try:
                    event = Event()
                    summary = row.get("标题", "No Summary")
                    date_str = str(row.get("日期"))
                    start_time_str = str(row.get("开始时间", "00:00"))
                    end_time_str = str(row.get("结束时间", "00:00"))
                    # 修改这一行，确保当备注为NaN时返回空字符串
                    description = row.get("备注", "")
                    if pd.isna(description):
                        description = ""
                    location = ""

                    try:
                        start_dt = datetime.strptime(
                            f"{date_str} {start_time_str}", "%Y.%m.%d %H:%M"
                        )
                        end_dt = datetime.strptime(
                            f"{date_str} {end_time_str}", "%Y.%m.%d %H:%M"
                        )
                    except ValueError:
                        print(
                            f"Warning: Could not parse date/time for row {index} in {filename}. Skipping event."
                        )
                        continue

                    event.add("summary", summary)
                    event.add("dtstart", start_dt)
                    event.add("dtend", end_dt)
                    event.add("description", description)
                    event.add("location", location)
                    event.add("uid", str(uuid.uuid4()))  # 在ICS生成时添加UID
                    cal.add_component(event)

                except Exception as e:
                    print(f"Error processing row {index} in {filename}: {e}")

            with open(ics_filepath, "wb") as f:
                f.write(cal.to_ical())
            print(f"Converted {filename} to {ics_filename}")

            # 添加腾讯云COS上传部分
            upload_to_cos(ics_filepath, ics_filename)

            # 刷新CDN目录缓存
            refresh_cdn_directory()

            # CalDAV 上传部分
            upload_ics_files(ics_filepath, ics_filename)


def upload_to_cos(ics_filepath, ics_filename):
    """
    上传ICS文件到腾讯云COS
    """
    # 检查文件是否在CALDAV_CREDENTIALS中
    if ics_filename not in CALDAV_CREDENTIALS:
        print(f"Skipping COS upload for {ics_filename}. Not in CALDAV_CREDENTIALS.")
        return False

    # 如果没有配置密钥，则跳过上传
    if not COS_SECRET_ID or not COS_SECRET_KEY:
        print(
            f"Skipping COS upload for {ics_filename}. Please configure SecretId/SecretKey in config.py."
        )
        return False

    try:
        # 配置COS客户端
        config = CosConfig(
            Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY
        )
        client = CosS3Client(config)

        # 上传文件
        response = client.upload_file(
            Bucket=COS_BUCKET,
            LocalFilePath=ics_filepath,
            Key=f"{COS_PATH}{ics_filename}",  # 存储在COS上的路径
        )

        print(f"Successfully uploaded {ics_filename} to COS bucket {COS_BUCKET}")
        return True
    except Exception as e:
        print(f"Error uploading {ics_filename} to COS: {e}")
        return False


def refresh_cdn_directory():
    """
    刷新腾讯云CDN目录缓存
    """
    try:
        # 实例化一个认证对象，入参需要传入腾讯云账户secretId，secretKey
        cred = credential.Credential(COS_SECRET_ID, COS_SECRET_KEY)

        # 实例化一个http选项，可选的，没有特殊需求可以跳过
        httpProfile = HttpProfile()
        httpProfile.endpoint = "cdn.tencentcloudapi.com"

        # 实例化一个client选项，可选的，没有特殊需求可以跳过
        clientProfile = ClientProfile()
        clientProfile.httpProfile = httpProfile

        # 实例化要请求产品的client对象，入参需要传入腾讯云账户secretId，secretKey
        client = cdn_client.CdnClient(cred, "", clientProfile)

        # 实例化一个请求对象，每个接口都会对应一个request对象
        req = models.PurgePathCacheRequest()

        # 填充请求参数，这里以刷新目录为例
        req.Paths = [CDN_URL]
        req.FlushType = "flush"

        # 通过client对象调用刷新目录缓存接口，返回响应
        resp = client.PurgePathCache(req)

        print(f"Successfully refreshed CDN cache for directory: {CDN_URL}")
        return True
    except Exception as e:
        print(f"Error refreshing CDN cache: {e}")
        return False


def upload_ics_files(ics_filepath, ics_filename):
    # 从配置文件获取CalDAV上传URL
    caldav_upload_url = CALDAV_UPLOAD_URL
    username = ""
    password = ""

    # 从配置文件获取凭据
    if ics_filename in CALDAV_CREDENTIALS:
        username = CALDAV_CREDENTIALS[ics_filename]["username"]
        password = CALDAV_CREDENTIALS[ics_filename]["password"]
    else:
        print(
            f"Skipping CalDAV upload for {ics_filename}. Please configure username/password in config.py."
        )
        return  # 跳过本次循环的上传

    if username and password:
        print(f"Attempting to upload {ics_filename} to CalDAV...")
        upload_ics_to_caldav(ics_filepath, caldav_upload_url, username, password)


def upload_ics_to_caldav(ics_filepath, caldav_url, username, password):
    try:
        client = caldav.DAVClient(url=caldav_url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            print(f"Error: No calendars found for {username} at {caldav_url}")
            return False

        calendar = calendars[0]

        # 清空日历中原有的内容
        print(f"Clearing existing events from calendar {calendar.name}...")
        try:
            # 使用 calendar.events() 获取所有事件
            for event in calendar.events():
                event.delete()  # caldav.Event 对象有 delete 方法
            print(
                f"Successfully cleared all existing events from calendar {calendar.name}."
            )
        except Exception as e:
            print(f"Error clearing existing events: {e}")
            # 如果清空失败，可以选择继续上传或直接返回
            # return False # 如果清空失败就停止上传

        with open(ics_filepath, "rb") as f:
            ics_content = f.read()

        cal = Calendar.from_ical(ics_content)

        uploaded_count = 0
        for component in cal.walk():
            if component.name == "VEVENT":
                event_ical = component.to_ical()
                event_uid = component.get("uid")
                # 确保UID存在且以.ics结尾，因为现在UID在ICS生成时就已添加
                if not event_uid:
                    print(
                        f"Warning: Event in {ics_filepath} missing UID. Skipping upload for this event."
                    )
                    continue

                resource_name = str(event_uid) + ".ics"  # 确保以.ics结尾

                try:
                    calendar.save_event(
                        ical=event_ical, overwrite=True, etag=None, path=resource_name
                    )
                    print(
                        f"Successfully uploaded event {event_uid} from {ics_filepath} to CalDAV at {caldav_url}"
                    )
                    uploaded_count += 1
                except Exception as e:
                    print(
                        f"Error uploading event {event_uid} from {ics_filepath} to CalDAV: {e}"
                    )

        if uploaded_count > 0:
            return True
        else:
            print(f"No events found or uploaded from {ics_filepath}.")
            return False

    except Exception as e:
        print(f"Error processing {ics_filepath} for CalDAV upload: {e}")
        return False


if __name__ == "__main__":
    input_dir = "../resources"
    output_dir = "../results"
    convert_csv_to_ics(input_dir, output_dir)
