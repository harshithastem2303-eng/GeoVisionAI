from pathlib import Path
import shutil
import random
import re

# ==========================================
# PATHS
# ==========================================

BASE_DIR = Path(__file__).parent

SOURCE_DIR = BASE_DIR / "person_crops"
DATASET_DIR = BASE_DIR / "dataset"

TRAIN_DIR = DATASET_DIR / "train" / "normal_person"
VAL_DIR = DATASET_DIR / "val" / "normal_person"
TEST_DIR = DATASET_DIR / "test" / "normal_person"


# ==========================================
# SETTINGS
# ==========================================

random.seed(42)

MAX_IMAGES_PER_ID = 20


# ==========================================
# CREATE DIRECTORIES
# ==========================================

for directory in [
    TRAIN_DIR,
    VAL_DIR,
    TEST_DIR
]:
    directory.mkdir(
        parents=True,
        exist_ok=True
    )


# ==========================================
# FIND CROPS
# ==========================================

images = list(
    SOURCE_DIR.glob("*.jpg")
)

print(
    f"Found {len(images)} person crops."
)


# ==========================================
# GROUP BY TRACK ID
# ==========================================

groups = {}

pattern = re.compile(
    r"id_(\d+)_"
)

for image in images:

    match = pattern.match(
        image.name
    )

    if not match:
        continue

    track_id = int(
        match.group(1)
    )

    groups.setdefault(
        track_id,
        []
    ).append(image)


print(
    f"Found {len(groups)} tracking IDs."
)


# ==========================================
# SELECT IMAGES
# ==========================================

selected = {}

for track_id, files in groups.items():

    # Shuffle files
    random.shuffle(files)

    # Limit images per person
    selected[track_id] = files[
        :MAX_IMAGES_PER_ID
    ]

    print(
        f"ID {track_id}: "
        f"{len(selected[track_id])} images selected"
    )


# ==========================================
# SPLIT TRACK IDs
# ==========================================

track_ids = list(
    selected.keys()
)

random.shuffle(track_ids)

total_ids = len(track_ids)

train_count = max(
    1,
    int(total_ids * 0.70)
)

val_count = max(
    1,
    int(total_ids * 0.15)
)

train_ids = track_ids[
    :train_count
]

val_ids = track_ids[
    train_count:
    train_count + val_count
]

test_ids = track_ids[
    train_count + val_count:
]


# ==========================================
# COPY FUNCTION
# ==========================================

def copy_images(
    ids,
    destination
):

    count = 0

    for track_id in ids:

        for image in selected[track_id]:

            new_name = (
                f"id_{track_id}_"
                f"{image.name}"
            )

            destination_file = (
                destination / new_name
            )

            shutil.copy2(
                image,
                destination_file
            )

            count += 1

    return count


# ==========================================
# COPY DATASET
# ==========================================

train_count_images = copy_images(
    train_ids,
    TRAIN_DIR
)

val_count_images = copy_images(
    val_ids,
    VAL_DIR
)

test_count_images = copy_images(
    test_ids,
    TEST_DIR
)


# ==========================================
# SUMMARY
# ==========================================

print()
print("=" * 50)
print("NORMAL PERSON DATASET CREATED")
print("=" * 50)

print(
    f"Train IDs: {len(train_ids)}"
)

print(
    f"Validation IDs: {len(val_ids)}"
)

print(
    f"Test IDs: {len(test_ids)}"
)

print()

print(
    f"Train images: {train_count_images}"
)

print(
    f"Validation images: {val_count_images}"
)

print(
    f"Test images: {test_count_images}"
)

print()
print(
    "Original person_crops were NOT modified."
)