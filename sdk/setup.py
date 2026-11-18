from setuptools import setup, find_packages
import os

readme_path = os.path.join(os.path.dirname(__file__), "README.md")
long_description = ""
if os.path.exists(readme_path):
    with open(readme_path, "r", encoding="utf-8") as f:
        long_description = f.read()

setup(
    name="brenducate-sdk",
    version="0.2.1",
    description="Per-tenant margin guardrails, same-family Smart Degradation, and sub-millisecond loop interception for OpenAI and Anthropic.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Brenducate",
    author_email="founder@brenducate.com",
    url="https://brenducate.com",
    packages=find_packages(include=["brenducate*"]),
    python_requires=">=3.9",
    install_requires=[
        "httpx>=0.27.0",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
