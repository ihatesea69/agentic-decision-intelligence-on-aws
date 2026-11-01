# Compute image for Markov and MDP (SageMaker Processing) and Monte Carlo + package (AWS Batch).
FROM public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/adi
RUN pip install --no-cache-dir numpy==2.2.6 scipy==1.15.3 boto3==1.42.2
WORKDIR /opt/adi
COPY engines/adi_engines ./adi_engines
COPY jobs/adi_jobs ./adi_jobs
RUN useradd --uid 10001 --create-home adi
USER adi
ENTRYPOINT ["python", "-m", "adi_jobs"]
