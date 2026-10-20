FROM ghcr.io/osgeo/gdal:ubuntu-full-3.13.1

RUN apt-get update && apt-get --assume-yes upgrade \
    && apt-get -qq install -y --no-install-recommends postgresql-common gnupg \
    && yes '' | sh /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh \
    && apt-get -qq install -y --no-install-recommends \
        postgresql-client-16 \
        git \
        unzip \
        curl \
        parallel \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(uname -m) \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}-2.22.21.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws

WORKDIR /home/harvest_restrictions
COPY requirements*.txt ./

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install -U pip && \
    /opt/venv/bin/python -m pip install --no-cache-dir -r requirements-dev.txt

ENV PATH="/opt/venv/bin:$PATH"
ENV AWS_PAGER=""