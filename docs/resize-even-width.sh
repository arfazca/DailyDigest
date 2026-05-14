#!/bin/zsh

# ==========================================
# Equalize PNG widths while preserving height
# Generates new files ending in -2.png
#
# Requirements:
# brew install imagemagick
# ==========================================

SCREENSHOT_DIR="/Users/arfaz/Desktop/Projects/DailyDigest/docs/screenshots"

# ------------------------------------------
# CHANGE THIS WIDTH
# ------------------------------------------
TARGET_WIDTH=1000
# ------------------------------------------

cd "$SCREENSHOT_DIR" || exit 1

echo ""
echo "Original image dimensions:"
echo "-----------------------------------"

# Remove old generated files
rm -f *-2.png

# Show current dimensions
for file in *.png; do
  [[ "$file" == *-2.png ]] && continue

  WIDTH=$(magick identify -format "%w" "$file")
  HEIGHT=$(magick identify -format "%h" "$file")

  printf "%-35s %4sx%-4s\n" "$file" "$WIDTH" "$HEIGHT"
done

echo ""
echo "Generating new images with width: ${TARGET_WIDTH}px"
echo "Original heights will remain unchanged"
echo "-----------------------------------"
echo ""

# Generate updated files
for file in *.png; do
  [[ "$file" == *-2.png ]] && continue

  NAME="${file%.png}"
  HEIGHT=$(magick identify -format "%h" "$file")

  magick "$file" \
    -background white \
    -gravity center \
    -extent "${TARGET_WIDTH}x${HEIGHT}" \
    "${NAME}-2.png"

  echo "Generated ${NAME}-2.png"
done

echo ""
echo "Done."
