"""
Setup configuration for Dibba - Container Orchestration Layer

Dibba is a lightweight, Python-based container orchestration layer built on:
- Celery for distributed task execution
- Redis for messaging and state
- Protobuf for typed RPC/data contracts
- containerd 2.0 for runtime management
- Calico for CNI networking
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read the contents of README file
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text(encoding="utf-8")

# Read requirements from requirements.txt
requirements_path = this_directory / "requirements.txt"
requirements = []
if requirements_path.exists():
    with open(requirements_path, "r", encoding="utf-8") as f:
        requirements = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

# Package metadata
setup(
    name="dibba",
    version="1.0.0",
    author="Dibba Contributors",
    author_email="",
    description="A lightweight, Python-based container orchestration layer",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/tkcreddy/dibba",
    project_urls={
        "Bug Reports": "https://github.com/tkcreddy/dibba/issues",
        "Source": "https://github.com/tkcreddy/dibba",
        "Documentation": "https://github.com/tkcreddy/dibba#readme",
    },
    packages=find_packages(exclude=[
        "tests",
        "tests.*",
        "archive",
        "archive.*",
        "venv",
        "venv.*",
        "*.tests",
        "*.tests.*",
        "tests.*",
        "cri-api",
        "cri-api.*",
        "gogo-protobuf",
        "gogo-protobuf.*",
    ]),
    include_package_data=True,
    package_data={
        "": [
            "*.md",
            "*.txt",
            "*.json",
            "*.proto",
            "*.sh",
        ],
    },
    python_requires=">=3.8",
    install_requires=requirements,
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "pytest-asyncio>=0.21.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
            "mypy>=1.0.0",
        ],
        "test": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "pytest-asyncio>=0.21.0",
            "pytest-mock>=3.10.0",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Distributed Computing",
        "Topic :: System :: Systems Administration",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: OS Independent",
        "Environment :: Console",
    ],
    keywords=[
        "container",
        "orchestration",
        "containerd",
        "celery",
        "redis",
        "kubernetes",
        "docker",
        "cni",
        "calico",
        "distributed",
        "task-queue",
    ],
    entry_points={
        "console_scripts": [
            "dibba-api=server.main_api:run_server",
        ],
    },
    zip_safe=False,
    platforms=["any"],
)

