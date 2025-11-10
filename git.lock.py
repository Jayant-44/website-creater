import os

repo_path = r"C:\Users\lenovo\OneDrive\Desktop\Internship_Project_2025\.git"
lock_file = os.path.join(repo_path, "index.lock")

if os.path.exists(lock_file):
    os.remove(lock_file)
    print("index.lock deleted ✅")
else:
    print("No index.lock found 🚀")
