"""
Run this script to reset your database with the new schema
Save as: reset_db.py
"""
import os
from app import app, db

def reset_database():
    """Delete old database and create new one with updated schema"""
    
    with app.app_context():
        # Path to database file
        db_path = 'instance/users.db'
        
        # Backup old database (optional)
        if os.path.exists(db_path):
            import shutil
            backup_path = f'{db_path}.backup'
            shutil.copy2(db_path, backup_path)
            print(f"✅ Backup created: {backup_path}")
            
            # Delete old database
            os.remove(db_path)
            print(f"🗑️  Deleted old database: {db_path}")
        
        # Create new database with updated schema
        db.create_all()
        print("✅ New database created with updated schema!")
        print("\n📋 Tables created:")
        print("   - User")
        print("   - SearchHistory (with category column)")
        
        print("\n⚠️  Note: All old data has been cleared.")
        print("   You'll need to create a new account.")

if __name__ == '__main__':
    reset_database()