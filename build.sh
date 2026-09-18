#!/bin/bash

set -e

echo "Installing/collecting Django static files..."

python manage.py collectstatic --noinput

echo "Static files collected successfully."