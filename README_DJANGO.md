# Ham Radio Database - Django Web Application

A Django web application for managing and browsing ham radio specifications with PostgreSQL database and Tailwind CSS styling.

## Features

- 📻 **Complete Radio Database**: Store brand, model, FCC ID, technical specs, and more
- 🔍 **Search & Filter**: Find radios by brand, model, or FCC ID
- ✏️ **CRUD Operations**: Create, read, update, and delete radio entries via web interface
- 📊 **Dashboard**: View statistics and recently added radios
- 🎨 **Modern UI**: Clean, responsive design with Tailwind CSS
- 📱 **Mobile Friendly**: Works on all device sizes
- 🗄️ **PostgreSQL**: Robust database with proper indexing
- 📥 **CSV Import**: Import your existing radio data from CSV files

## Prerequisites

- Python 3.10 - 3.13 (Python 3.14 not yet fully supported by Django)
- PostgreSQL 14+
- Node.js 16+ (for Tailwind CSS)
- npm (comes with Node.js)

## Installation

### 1. Set Up PostgreSQL Database

```bash
# Create database
createdb radio_database

# Or using psql
psql -U postgres
CREATE DATABASE radio_database;
\q
```

### 2. Set Up Python Environment

```bash
cd radio_database

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate  # On macOS/Linux
# or
venv\Scripts\activate  # On Windows

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file (optional) or set environment variables:

```bash
export DB_NAME=radio_database
export DB_USER=ashaw  # or your PostgreSQL username
export DB_PASSWORD=  # leave empty if using local peer authentication
export DB_HOST=localhost
export DB_PORT=5432
```

Or edit `radio_database/settings.py` directly with your database credentials.

### 4. Set Up Tailwind CSS

```bash
# Initialize Tailwind theme
python manage.py tailwind install

# This will install Node.js dependencies for Tailwind CSS
```

### 5. Run Migrations

```bash
python manage.py makemigrations
python manage.py migrate
```

### 6. Create Superuser (for Admin Access)

```bash
python manage.py createsuperuser
```

Follow the prompts to create an admin account.

### 7. Import Radio Data (Optional)

If you have the CSV file with radio data:

```bash
# Import radios from CSV (path relative to parent directory)
python manage.py import_radios ../merged_master_with_fcc.csv

# Or clear existing data first
python manage.py import_radios ../merged_master_with_fcc.csv --clear
```

## Running the Application

### Start the Development Server

In one terminal, start the Tailwind CSS compiler:

```bash
python manage.py tailwind start
```

In another terminal, start the Django development server:

```bash
python manage.py runserver
```

The application will be available at:
- **Main App**: http://localhost:8000/
- **Admin Interface**: http://localhost:8000/admin/

## Usage

### Web Interface

1. **Dashboard** (`/`): View statistics and recently added radios
2. **All Radios** (`/radios/`): Browse, search, and filter all radios
3. **Add Radio** (`/radios/add/`): Create a new radio entry
4. **View Details** (`/radios/<id>/`): See complete specifications
5. **Edit Radio** (`/radios/<id>/edit/`): Update radio information
6. **Delete Radio** (`/radios/<id>/delete/`): Remove a radio entry

### Admin Interface

Access the Django admin at `/admin/` for advanced database management:
- Bulk operations
- Advanced filtering
- Data export
- User management

## Project Structure

```
radio_database/
├── manage.py                 # Django management script
├── requirements.txt          # Python dependencies
├── radio_database/          # Project settings
│   ├── settings.py          # Django configuration
│   ├── urls.py              # Root URL configuration
│   └── wsgi.py              # WSGI configuration
└── radios/                  # Main application
    ├── models.py            # Radio data model
    ├── views.py             # View logic (CRUD operations)
    ├── forms.py             # Django forms
    ├── urls.py              # App URL routing
    ├── admin.py             # Admin configuration
    ├── templates/           # HTML templates
    │   ├── base.html        # Base template with navigation
    │   └── radios/          # Radio-specific templates
    │       ├── dashboard.html
    │       ├── radio_list.html
    │       ├── radio_detail.html
    │       ├── radio_form.html
    │       └── radio_confirm_delete.html
    └── management/
        └── commands/
            └── import_radios.py  # CSV import command
```

## Database Schema

### Radio Model Fields

- **brand**: Manufacturer/brand name (indexed)
- **model**: Model name/number (indexed with brand)
- **fcc_id**: FCC ID (e.g., 2AJGM-UV5R)
- **grantee_code**: FCC Grantee Code
- **frequency_range**: Operating frequency range
- **power_output**: Transmit power
- **modulation**: Modulation types (FM, AM, SSB, etc.)
- **bands**: Band support (VHF/UHF, etc.)
- **digital_modes**: Digital modes (DMR, D-STAR, etc.)
- **channels**: Number of memory channels
- **notes**: Additional notes
- **review_url**: Link to eHam.net or other reviews
- **created_at**: Creation timestamp
- **updated_at**: Last update timestamp

## Development

### Running Tests

```bash
python manage.py test
```

### Creating Database Backups

```bash
python manage.py dumpdata radios.Radio --indent 2 > backup.json
```

### Restoring from Backup

```bash
python manage.py loaddata backup.json
```

## Production Deployment

For production deployment:

1. Set `DEBUG = False` in settings.py
2. Configure a proper `SECRET_KEY`
3. Update `ALLOWED_HOSTS`
4. Set up static file serving
5. Use a production WSGI server (gunicorn, uwsgi)
6. Configure PostgreSQL for production
7. Set up HTTPS/SSL
8. Use environment variables for sensitive settings

## Troubleshooting

### Tailwind CSS not compiling

```bash
# Reinstall Tailwind dependencies
python manage.py tailwind install
```

### Database connection errors

- Check PostgreSQL is running: `pg_isready`
- Verify credentials in settings.py
- Ensure database exists: `psql -l`

### Import errors

- Verify CSV file path
- Check CSV column names match expected format
- Review error messages for specific issues

## License

This project is for personal/educational use.

## Support

For issues or questions, please review the Django documentation:
- Django: https://docs.djangoproject.com/
- Django Tailwind: https://django-tailwind.readthedocs.io/
- PostgreSQL: https://www.postgresql.org/docs/
