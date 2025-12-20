# Dibba Setup.py Guide

This guide explains how to use the `setup.py` file to install and distribute the Dibba package.

## Overview

The `setup.py` file provides:
- Package metadata and description
- Dependency management from `requirements.txt`
- Entry points for console scripts
- Package discovery and inclusion
- Development and test dependencies

## Installation Methods

### 1. Development Installation (Editable Mode)

Install the package in development/editable mode. Changes to source code are immediately available:

```bash
pip install -e .
```

Or with development dependencies:

```bash
pip install -e ".[dev]"
```

### 2. Regular Installation

Install the package normally:

```bash
pip install .
```

### 3. Installation with Test Dependencies

Install with test dependencies:

```bash
pip install ".[test]"
```

### 4. Installation with All Extras

Install with both dev and test dependencies:

```bash
pip install ".[dev,test]"
```

## Building Distribution Packages

### Build Source Distribution

Create a source distribution (`.tar.gz`):

```bash
python setup.py sdist
```

### Build Wheel Distribution

Create a wheel distribution (`.whl`):

```bash
python setup.py bdist_wheel
```

### Build Both

```bash
python setup.py sdist bdist_wheel
```

## Installing from Distribution

### From Source Distribution

```bash
pip install dist/dibba-1.0.0.tar.gz
```

### From Wheel

```bash
pip install dist/dibba-1.0.0-py3-none-any.whl
```

## Using Entry Points

After installation, you can use the console script entry point:

```bash
dibba-api
```

This will start the Dibba API server using uvicorn.

## Package Structure

The setup.py includes:
- **Packages**: All Python packages in the project (server, utils, logpkg, etc.)
- **Exclusions**: Tests, archive, venv, and other non-distributable directories
- **Package Data**: Markdown files, JSON configs, proto files, shell scripts
- **Dependencies**: All packages from `requirements.txt`

## Development Dependencies

The `[dev]` extra includes:
- `pytest` - Testing framework
- `pytest-cov` - Coverage reporting
- `pytest-asyncio` - Async test support
- `black` - Code formatter
- `flake8` - Linter
- `mypy` - Type checker

## Test Dependencies

The `[test]` extra includes:
- `pytest` - Testing framework
- `pytest-cov` - Coverage reporting
- `pytest-asyncio` - Async test support
- `pytest-mock` - Mocking utilities

## Verification

### Check Setup Configuration

```bash
python setup.py check
```

### List Package Contents

```bash
python setup.py --help-commands
```

### Show Package Information

```bash
python setup.py --name
python setup.py --version
```

## Publishing to PyPI (Optional)

If you want to publish to PyPI:

1. **Build distributions**:
   ```bash
   python setup.py sdist bdist_wheel
   ```

2. **Check distributions**:
   ```bash
   twine check dist/*
   ```

3. **Upload to TestPyPI** (for testing):
   ```bash
   twine upload --repository-url https://test.pypi.org/legacy/ dist/*
   ```

4. **Upload to PyPI**:
   ```bash
   twine upload dist/*
   ```

## Troubleshooting

### Import Errors After Installation

If you get import errors, ensure:
- The package is installed: `pip list | grep dibba`
- You're using the correct Python environment
- Package data is included (check `MANIFEST.in`)

### Missing Files

If files are missing from the distribution:
- Check `MANIFEST.in` includes the necessary patterns
- Verify files exist in the source directory
- Rebuild the distribution

### Entry Point Not Found

If `dibba-api` command is not found:
- Ensure the package is installed: `pip install -e .`
- Check your PATH includes the Python scripts directory
- Verify the entry point in `setup.py`

## Files Included

- `setup.py` - Main setup configuration
- `MANIFEST.in` - Additional files to include in distribution
- `requirements.txt` - Runtime dependencies
- `README.md` - Project documentation (included in package)

## Notes

- The package requires Python 3.8 or higher
- All dependencies are read from `requirements.txt`
- The package excludes test files, archive, and venv from distribution
- Entry point `dibba-api` runs the FastAPI server

---

*Last Updated: December 2024*

