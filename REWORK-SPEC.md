# Rework Story Format

A rework story is a targeted improvement to a specific function or class in an existing file.

## Fields

- `target_file`: Path to the file containing the code to modify
- `target_function`: Name of the function or class to improve
- `desired_behavior`: Description of the improved behavior
- `preserve`: List of behaviors or constraints that must be maintained
- `acceptance_criteria`: List of criteria to verify the change

## Rules

1. Always use `edit_file` (not `write_file`) for rework stories. Edit only the function being improved.
2. **Acceptance criteria must be shell commands — not prose.** Ralph games prose ACs.
   - ❌ Bad: `"sandbox/ralph.sh contains 'AUTO_CRITIQUE' string"`
   - ✅ Good: `"grep -q 'AUTO_CRITIQUE' sandbox/ralph.sh"`
   - ✅ Good: `"python3 -m py_compile sandbox/tools.py"`
   - ✅ Good: `"bash -n sandbox/ralph.sh"`
   The prd_linter.py will reject ACs that don't contain a recognizable shell command.

## Example

```json
{
  "id": "CR-001",
  "title": "Improve validate_user function",
  "type": "rework",
  "target_file": "src/auth.py",
  "target_function": "validate_user",
  "desired_behavior": "Add email format validation",
  "preserve": ["existing username check", "return format"],
  "acceptance_criteria": [
    "Returns false for invalid email format",
    "Returns true for valid email format",
    "Existing validation still works"
  ]
}
```
