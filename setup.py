import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="twsq",
    version="1.0.0",
    author="thewallstreetquants",
    author_email="admin@thewallstreetquants.com",
    description="Quant Trading Package",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=setuptools.find_packages(),

    install_requires=[
        "ccxt",
        "pandas",
        "numpy",
        "websocket-client",
        "python-telegram-handler",
        "python-telegram-bot",
        "multidict >=4.5,<5.0"
    ],

    classifiers=(
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ),
    
    python_requires=">=3.0",
    
)
