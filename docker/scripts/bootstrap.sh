#!/bin/bash

gen_key() {
  echo "=================================================================================="
  echo "Pinry secret-key generation completed."

  SECRET_KEY=$(bash /pinry/docker/scripts/gen_key.sh)

  echo "=================================================================================="
}

local_settings_file="/data/local_settings.py"
# Create local_settings.py
if [ ! -f "${local_settings_file}" ];
then
    cp "/pinry/pinry/settings/local_settings.example.py" "${local_settings_file}"
    gen_key
    sed -i "s/secret\_key\_place\_holder/${SECRET_KEY}/" "${local_settings_file}"
fi

cp "${local_settings_file}" "/pinry/pinry/settings/local_settings.py"
