#!/bin/sh
celery -A utils.celery.aws_worker flower --port=5555
