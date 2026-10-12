with open("benchmarks/data/generate_200_cases.py", "r", encoding="utf-8") as f:
    code = f.read()

code = code.replace('"is_attack": true,', '"is_attack": True,')
code = code.replace('"is_attack": false,', '"is_attack": False,')

with open("benchmarks/data/generate_200_cases.py", "w", encoding="utf-8") as f:
    f.write(code)

print("generate_200_cases.py successfully corrected!")
import sys
sys.path.insert(0, ".")
from benchmarks.data.generate_200_cases import generate_200_security_cases
generate_200_security_cases()
