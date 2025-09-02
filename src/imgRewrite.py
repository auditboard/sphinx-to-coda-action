#!/usr/bin/env python3

import pathlib
import uuid
import logging

def fn_imgRewrite(s3Bucket, s3Client, filepath, timeout=1200, s3prefix=""):

    """
    Does S3 Rewrites if Configured

    :param s3Bucket: S3 Bucket Name
    :type s3Bucket: str
    :param s3Client: S3 Client Object
    :type s3Client: S3.Client
    :param filepath: File Path to Image
    :type filepath: str, pathlib.Path
    :param timeout: Timeout for S3 URL, defaults to 1200
    :type timeout: int, optional
    :param s3prefix: S3 Prefix for Uploaded Objects, defaults to ''
    :type s3prefix: str

    :returns: Best Url as a String
    :rtype: str
    """

    logger = logging.getLogger("imgRewrite.py")

    orig_uri = filepath

    filepath_obj = pathlib.Path(filepath)

    s3_key = "{}{}{}".format(s3prefix, str(uuid.uuid4()), filepath_obj.suffix)

    try:
        with open(orig_uri, 'rb') as img_data:
            # Upload File
            s3Client.put_object(
                Bucket=s3Bucket,
                Key=s3_key,
                Body=img_data,
            )

        psigned_get = s3Client.generate_presigned_url(
            'get_object',
            Params={'Bucket': s3Bucket, 'Key': s3_key},
            ExpiresIn=timeout
        )
    except Exception as e:
        logger.error("Unable to Upload Image to S3 : {}".format(filepath))
        logger.debug("Error: {}".format(e))
        return filepath
    else:
        logger.info("Uploaded Image to S3 @ {}".format(s3_key))
        return psigned_get