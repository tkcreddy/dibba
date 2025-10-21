#!/bin/sh
celery -A utils.celery.worker_node worker -l info
