from setuptools import setup, find_packages

setup(
    name="nest_tcp",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[],
    author="Mainak Debnath",
    author_email="mainak.debnath@icloud.com",
    description="NestJS-style TCP client for FastAPI",
    url="https://github.com/BurnedOx/nest-tcp.git",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.7',
)
