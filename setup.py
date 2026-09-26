from pathlib import Path

from setuptools import find_packages, setup


BASE_DIR = Path(__file__).resolve().parent


setup(
	name="vic-voices",
	version="0.1.0",
	description="Local CPU-friendly AI voice conversion web application",
	packages=find_packages(include=["pkg", "pkg.*"]),
	py_modules=["run"],
	include_package_data=True,
	package_data={
		"pkg": [
			"templates/*.html",
			"templates/admin/*.html",
			"templates/user/*.html",
			"static/**/*",
		]
	},
	python_requires=">=3.10",
	install_requires=[
		"Flask>=3.1,<4",
		"Flask-Login>=0.6,<1",
		"Flask-Migrate>=4,<5",
		"Flask-SQLAlchemy>=3.1,<4",
		"Flask-WTF>=1.2,<2",
		"email-validator>=2,<3",
		"gunicorn>=23,<24",
		"mysql-connector-python>=9,<10",
		"kokoro>=0.9,<1",
		"soundfile>=0.13,<1",
		"python-dotenv>=1,<2",
		"requests>=2.30,<3",
		"WTForms>=3.1,<4",
	],
	extras_require={
		"test": ["pytest>=8,<10"],
	},
	entry_points={
		"console_scripts": [
			"vic-voices=run:main",
		],
	},
)
