FROM python:3.12-bookworm
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY . /app

# config nodejs
RUN curl -L https://raw.githubusercontent.com/tj/n/master/bin/n -o n
RUN bash n 18
RUN npm -g install pnpm@9.15.9

WORKDIR /app
RUN pip install poetry==1.8.5
RUN poetry install
RUN rm -fr /app/*
