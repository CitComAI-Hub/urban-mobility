##############################################
# KAFKA + MINIO + REDIS CONFIGURATION #######
##############################################
"""
Configuration and helper functions for Kafka, MinIO, and Redis integration.

This module provides:
- Connection management for Kafka, MinIO, and Redis
- Helper functions for publishing/consuming messages
- File upload/download to/from MinIO
- Job state management in Redis
"""

import os
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
import asyncio

from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from minio import Minio
from minio.error import S3Error
import redis

logger = logging.getLogger(__name__)


# ============================================
# CONFIGURATION
# ============================================

class Config:
    """Configuration from environment variables."""

    # Kafka configuration
    KAFKA_BOOTSTRAP_SERVERS = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'kafka:29092')
    KAFKA_JOB_REQUESTS_TOPIC = os.environ.get('KAFKA_JOB_REQUESTS_TOPIC', 'job-requests')
    KAFKA_JOB_STATUS_TOPIC = os.environ.get('KAFKA_JOB_STATUS_TOPIC', 'job-status')
    KAFKA_CONSUMER_GROUP = os.environ.get('KAFKA_CONSUMER_GROUP', 'inference-workers')

    # MinIO configuration
    MINIO_ENDPOINT = os.environ.get('MINIO_ENDPOINT', 'minio:9000')
    MINIO_ACCESS_KEY = os.environ.get('MINIO_ACCESS_KEY', 'minioadmin')
    MINIO_SECRET_KEY = os.environ.get('MINIO_SECRET_KEY', 'minioadmin')
    MINIO_SECURE = os.environ.get('MINIO_SECURE', 'false').lower() == 'true'
    MINIO_BUCKET = os.environ.get('MINIO_BUCKET', 'inference-data')

    # Redis configuration
    REDIS_HOST = os.environ.get('REDIS_HOST', 'redis')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', '6379'))
    REDIS_DB = int(os.environ.get('REDIS_DB', '0'))
    REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)

    # Job retention
    JOB_RETENTION_SECONDS = int(os.environ.get('JOB_RETENTION_SECONDS', str(24 * 3600)))  # 24 hours


# ============================================
# KAFKA CLIENT
# ============================================

class KafkaClient:
    """Async Kafka producer and consumer wrapper."""

    def __init__(self):
        self.producer: Optional[AIOKafkaProducer] = None
        self.consumer: Optional[AIOKafkaConsumer] = None

    async def connect_producer(self):
        """Connect Kafka producer."""
        if self.producer is None:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=Config.KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode('utf-8')
            )
            await self.producer.start()
            logger.info(f"Kafka producer connected to {Config.KAFKA_BOOTSTRAP_SERVERS}")

    async def connect_consumer(self):
        """Connect Kafka consumer for job requests."""
        if self.consumer is None:
            self.consumer = AIOKafkaConsumer(
                Config.KAFKA_JOB_REQUESTS_TOPIC,
                bootstrap_servers=Config.KAFKA_BOOTSTRAP_SERVERS,
                group_id=Config.KAFKA_CONSUMER_GROUP,
                value_deserializer=lambda m: json.loads(m.decode('utf-8')),
                auto_offset_reset='latest',  # Only process new jobs
                enable_auto_commit=True
            )
            await self.consumer.start()
            logger.info(f"Kafka consumer connected to {Config.KAFKA_BOOTSTRAP_SERVERS}")

    async def publish_job_request(self, job_id: str, job_data: Dict[str, Any]):
        """Publish new job request to Kafka."""
        await self.connect_producer()

        message = {
            'job_id': job_id,
            'data': job_data,
            'created_at': datetime.now(timezone.utc).isoformat()
        }

        await self.producer.send_and_wait(
            Config.KAFKA_JOB_REQUESTS_TOPIC,
            value=message,
            key=job_id.encode('utf-8')
        )
        logger.info(f"Published job request: {job_id}")

    async def publish_job_status(self, job_id: str, status_data: Dict[str, Any]):
        """Publish job status update to Kafka."""
        await self.connect_producer()

        message = {
            'job_id': job_id,
            'status': status_data,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }

        await self.producer.send_and_wait(
            Config.KAFKA_JOB_STATUS_TOPIC,
            value=message,
            key=job_id.encode('utf-8')
        )
        logger.debug(f"Published status update for job {job_id}: {status_data.get('status')}")

    async def consume_job_requests(self):
        """Consume job requests from Kafka (generator)."""
        await self.connect_consumer()

        async for message in self.consumer:
            yield message.value

    async def close(self):
        """Close Kafka connections."""
        if self.producer:
            await self.producer.stop()
        if self.consumer:
            await self.consumer.stop()
        logger.info("Kafka connections closed")


# ============================================
# MINIO CLIENT
# ============================================

class MinioClient:
    """MinIO client for large file storage."""

    def __init__(self):
        self.client = Minio(
            Config.MINIO_ENDPOINT,
            access_key=Config.MINIO_ACCESS_KEY,
            secret_key=Config.MINIO_SECRET_KEY,
            secure=Config.MINIO_SECURE
        )
        self.bucket = Config.MINIO_BUCKET
        self._ensure_bucket()

    def _ensure_bucket(self):
        """Ensure bucket exists."""
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                logger.info(f"Created MinIO bucket: {self.bucket}")
            else:
                logger.info(f"MinIO bucket exists: {self.bucket}")
        except S3Error as e:
            logger.error(f"Failed to ensure bucket exists: {e}")
            raise

    def upload_file(self, object_name: str, file_path: str, content_type: str = 'application/octet-stream') -> str:
        """
        Upload file to MinIO.

        Args:
            object_name: Object name in MinIO (e.g., 'jobs/abc-123/input.json')
            file_path: Local file path to upload
            content_type: MIME type

        Returns:
            Object URL for reference
        """
        try:
            self.client.fput_object(
                self.bucket,
                object_name,
                file_path,
                content_type=content_type
            )
            url = f"minio://{self.bucket}/{object_name}"
            logger.info(f"Uploaded file to MinIO: {url}")
            return url
        except S3Error as e:
            logger.error(f"Failed to upload file to MinIO: {e}")
            raise

    def upload_bytes(self, object_name: str, data: bytes, content_type: str = 'application/octet-stream') -> str:
        """
        Upload bytes to MinIO.

        Args:
            object_name: Object name in MinIO
            data: Bytes to upload
            content_type: MIME type

        Returns:
            Object URL for reference
        """
        import io
        try:
            self.client.put_object(
                self.bucket,
                object_name,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type
            )
            url = f"minio://{self.bucket}/{object_name}"
            logger.info(f"Uploaded bytes to MinIO: {url}")
            return url
        except S3Error as e:
            logger.error(f"Failed to upload bytes to MinIO: {e}")
            raise

    def download_file(self, object_name: str, file_path: str):
        """
        Download file from MinIO.

        Args:
            object_name: Object name in MinIO
            file_path: Local file path to save
        """
        try:
            self.client.fget_object(
                self.bucket,
                object_name,
                file_path
            )
            logger.info(f"Downloaded file from MinIO: {object_name}")
        except S3Error as e:
            logger.error(f"Failed to download file from MinIO: {e}")
            raise

    def download_bytes(self, object_name: str) -> bytes:
        """
        Download bytes from MinIO.

        Args:
            object_name: Object name in MinIO

        Returns:
            File contents as bytes
        """
        try:
            response = self.client.get_object(self.bucket, object_name)
            data = response.read()
            response.close()
            response.release_conn()
            logger.info(f"Downloaded bytes from MinIO: {object_name}")
            return data
        except S3Error as e:
            logger.error(f"Failed to download bytes from MinIO: {e}")
            raise

    def get_presigned_url(self, object_name: str, expires: timedelta = timedelta(hours=1)) -> str:
        """
        Get presigned URL for temporary access.

        Args:
            object_name: Object name in MinIO
            expires: URL expiration time

        Returns:
            Presigned URL
        """
        try:
            url = self.client.presigned_get_object(
                self.bucket,
                object_name,
                expires=expires
            )
            logger.info(f"Generated presigned URL for {object_name}")
            return url
        except S3Error as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            raise

    def delete_object(self, object_name: str):
        """Delete object from MinIO."""
        try:
            self.client.remove_object(self.bucket, object_name)
            logger.info(f"Deleted object from MinIO: {object_name}")
        except S3Error as e:
            logger.error(f"Failed to delete object from MinIO: {e}")
            raise


# ============================================
# REDIS CLIENT
# ============================================

class RedisClient:
    """Redis client for job state management."""

    def __init__(self):
        self.client = redis.Redis(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            db=Config.REDIS_DB,
            password=Config.REDIS_PASSWORD,
            decode_responses=True
        )
        self._test_connection()

    def _test_connection(self):
        """Test Redis connection."""
        try:
            self.client.ping()
            logger.info(f"Redis connected to {Config.REDIS_HOST}:{Config.REDIS_PORT}")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    def set_job_status(self, job_id: str, status_data: Dict[str, Any], ttl: Optional[int] = None):
        """
        Set job status in Redis.

        Args:
            job_id: Job identifier
            status_data: Job status data
            ttl: Time to live in seconds (None = use default retention)
        """
        key = f"job:{job_id}"
        value = json.dumps(status_data)

        if ttl is None:
            ttl = Config.JOB_RETENTION_SECONDS

        self.client.setex(key, ttl, value)
        logger.debug(f"Set job status in Redis: {job_id}")

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get job status from Redis.

        Args:
            job_id: Job identifier

        Returns:
            Job status data or None if not found
        """
        key = f"job:{job_id}"
        value = self.client.get(key)

        if value is None:
            return None

        return json.loads(value)

    def delete_job(self, job_id: str):
        """Delete job from Redis."""
        key = f"job:{job_id}"
        self.client.delete(key)
        logger.debug(f"Deleted job from Redis: {job_id}")

    def list_jobs(self, pattern: str = "job:*", limit: int = 1000) -> list:
        """
        List all jobs matching pattern.

        Args:
            pattern: Redis key pattern
            limit: Maximum number of jobs to return

        Returns:
            List of job data
        """
        keys = list(self.client.scan_iter(match=pattern, count=limit))
        jobs = []

        for key in keys[:limit]:
            value = self.client.get(key)
            if value:
                jobs.append(json.loads(value))

        return jobs

    def close(self):
        """Close Redis connection."""
        self.client.close()
        logger.info("Redis connection closed")


# ============================================
# HELPER FUNCTIONS
# ============================================

def parse_minio_url(url: str) -> tuple:
    """
    Parse MinIO URL into bucket and object name.

    Args:
        url: MinIO URL (e.g., 'minio://bucket/path/to/object')

    Returns:
        Tuple of (bucket, object_name)
    """
    if not url.startswith('minio://'):
        raise ValueError(f"Invalid MinIO URL: {url}")

    parts = url[8:].split('/', 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else ''

    return bucket, object_name


def generate_object_name(job_id: str, file_type: str, extension: str = '') -> str:
    """
    Generate MinIO object name for job files.

    Args:
        job_id: Job identifier
        file_type: Type of file (e.g., 'input', 'output', 'result')
        extension: File extension (e.g., '.json', '.png')

    Returns:
        Object name (e.g., 'jobs/abc-123/input.json')
    """
    return f"jobs/{job_id}/{file_type}{extension}"
