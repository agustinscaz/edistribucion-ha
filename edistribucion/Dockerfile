ARG BUILD_FROM
FROM $BUILD_FROM

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev \
    && npx playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8099
CMD ["/run.sh"]
