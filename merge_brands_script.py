import os
import django
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "radio_database.settings")
django.setup()

from radios.models import Brand, Radio

def merge():
    try:
        primary = Brand.objects.get(name="Quanzhou Wouxun Electronics Co., Ltd.")
        secondary = Brand.objects.get(name="Wouxun")
    except Brand.DoesNotExist:
        print("Brands not found")
        return

    # Move radios
    Radio.objects.filter(brand="Quanzhou Wouxun Electronics Co., Ltd.").update(brand="Wouxun")
    
    # We probably want Wouxun as the primary name since it's the alias everyone uses
    secondary.full_name = primary.name
    secondary.save()
    
    # delete the old one
    primary.delete()
    print("Merged Wouxun successfully!")
    
if __name__ == "__main__":
    merge()
