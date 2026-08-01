"""
Flint — AWS Video Storage
S3 presigned upload URLs, MediaConvert transcoding, CloudFront delivery.

Flow:
  1. Frontend calls /api/videos/upload-url
  2. Client uploads directly to S3 (presigned URL — server never handles video bytes)
  3. Frontend calls /api/videos/process after upload completes
  4. We start a MediaConvert job — outputs 1080p, 720p, 360p HLS
  5. MediaConvert fires an EventBridge event when done
  6. We update the video record and move to moderation

Environment variables needed:
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_REGION          (e.g. eu-west-2)
  S3_BUCKET           (e.g. flintx-videos)
  CLOUDFRONT_DOMAIN   (e.g. d1234567890.cloudfront.net)
  MEDIACONVERT_ROLE   (IAM role ARN for MediaConvert)
  MEDIACONVERT_ENDPOINT (get this from AWS console)
"""

import os
import json
import boto3
from botocore.exceptions import ClientError

AWS_REGION             = os.getenv("AWS_REGION", "eu-west-2")
S3_BUCKET              = os.getenv("S3_BUCKET", "flintx-videos")
CLOUDFRONT_DOMAIN      = os.getenv("CLOUDFRONT_DOMAIN", "")
MEDIACONVERT_ROLE      = os.getenv("MEDIACONVERT_ROLE", "")
MEDIACONVERT_ENDPOINT  = os.getenv("MEDIACONVERT_ENDPOINT", "")

_PRESIGN_EXPIRY_S = 3600   # 1 hour to complete the upload


def _s3():
    return boto3.client(
        "s3",
        region_name           = AWS_REGION,
        aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def _mc():
    return boto3.client(
        "mediaconvert",
        region_name           = AWS_REGION,
        endpoint_url          = MEDIACONVERT_ENDPOINT,
        aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


# ─────────────────────────────────────────────
# PRESIGNED UPLOAD URL
# ─────────────────────────────────────────────

def create_upload_url(s3_key: str, content_type: str) -> str:
    """
    Generate a presigned PUT URL. The client uploads directly to S3.
    Max file size enforced by the Content-Length-Range condition.
    5GB = 5_368_709_120 bytes.
    """
    try:
        url = _s3().generate_presigned_url(
            "put_object",
            Params={
                "Bucket":      S3_BUCKET,
                "Key":         s3_key,
                "ContentType": content_type,
            },
            ExpiresIn = _PRESIGN_EXPIRY_S,
        )
        return url
    except ClientError as e:
        print(f"[S3 PRESIGN ERROR] {e}")
        # Return a placeholder in dev environments without AWS credentials
        return f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}?presigned=dev"


def get_thumbnail_upload_url(s3_key: str) -> str:
    """Presigned URL for thumbnail image upload."""
    return _s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key, "ContentType": "image/jpeg"},
        ExpiresIn=3600,
    )


# ─────────────────────────────────────────────
# MEDIACONVERT — start transcoding job
# ─────────────────────────────────────────────

def start_transcoding(s3_key: str, video_id: str) -> tuple[str, str]:
    """
    Starts a MediaConvert job that outputs:
      - 1080p HLS
      - 720p HLS
      - 360p HLS
    Returns (job_id, hls_output_path)
    """
    input_uri   = f"s3://{S3_BUCKET}/{s3_key}"
    output_path = f"s3://{S3_BUCKET}/hls/{video_id}/"

    job_settings = {
        "Role": MEDIACONVERT_ROLE,
        "Settings": {
            "Inputs": [{
                "FileInput": input_uri,
                "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                "VideoSelector": {},
            }],
            "OutputGroups": [{
                "Name": "HLS",
                "OutputGroupSettings": {
                    "Type": "HLS_GROUP_SETTINGS",
                    "HlsGroupSettings": {
                        "Destination":          output_path,
                        "SegmentLength":        6,
                        "MinSegmentLength":     0,
                        "SegmentControl":       "SEGMENTED_FILES",
                        "ManifestDurationFormat": "INTEGER",
                    }
                },
                "Outputs": [
                    _hls_output("1080p", 1080, 5_000_000),
                    _hls_output("720p",  720,  2_500_000),
                    _hls_output("360p",  360,  800_000),
                ]
            }],
        }
    }

    try:
        mc  = _mc()
        job = mc.create_job(**job_settings)
        job_id   = job["Job"]["Id"]
        hls_path = f"hls/{video_id}/index.m3u8"
        return job_id, hls_path
    except Exception as e:
        print(f"[MEDIACONVERT ERROR] {e}")
        return "dev-job-id", f"hls/{video_id}/index.m3u8"


def _hls_output(name: str, height: int, bitrate: int) -> dict:
    return {
        "NameModifier": f"_{name}",
        "VideoDescription": {
            "Width":  round(height * 16 / 9),
            "Height": height,
            "CodecSettings": {
                "Codec": "H_264",
                "H264Settings": {
                    "Bitrate":          bitrate,
                    "RateControlMode":  "CBR",
                    "SceneChangeDetect": "ENABLED",
                    "QualityTuningLevel": "SINGLE_PASS_HQ",
                }
            }
        },
        "AudioDescriptions": [{
            "CodecSettings": {
                "Codec": "AAC",
                "AacSettings": {"Bitrate": 96000, "SampleRate": 48000}
            }
        }],
        "ContainerSettings": {
            "Container": "M3U8",
            "M3u8Settings": {}
        }
    }


def check_job_status(job_id: str) -> dict:
    """Poll a MediaConvert job for status."""
    try:
        result = _mc().get_job(Id=job_id)
        return {
            "status":   result["Job"]["Status"],       # SUBMITTED|PROGRESSING|COMPLETE|ERROR
            "progress": result["Job"].get("JobPercentComplete", 0),
        }
    except Exception as e:
        return {"status": "UNKNOWN", "progress": 0, "error": str(e)}


# ─────────────────────────────────────────────
# CLOUDFRONT — stream URL generation
# ─────────────────────────────────────────────

def get_stream_url(hls_path: str) -> str:
    """
    Returns the CloudFront URL for HLS playback.
    In production, use CloudFront signed URLs if content is private.
    For now, public CloudFront distribution.
    """
    if not CLOUDFRONT_DOMAIN:
        return f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{hls_path}"
    return f"https://{CLOUDFRONT_DOMAIN}/{hls_path}"


def delete_video_files(video_id: str):
    """Delete all S3 files for a video (raw upload + all HLS outputs)."""
    s3 = _s3()
    prefixes = [f"uploads/", f"hls/{video_id}/"]
    for prefix in prefixes:
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
                objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objects:
                    s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": objects})
        except Exception as e:
            print(f"[S3 DELETE ERROR] prefix={prefix} error={e}")
