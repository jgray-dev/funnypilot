# Saving recordings in the web dashboard

Open **Drives** and select **Save drive**, either in the list or during
playback. **Saved · Unsave** means automatic cleanup skips the whole drive,
including later segments if it is still recording. Use **Saved drives** to
filter the list. Saved state survives restarts and branch updates that support
this feature; older releases do not enforce it.

The existing background deleter checks every 60 seconds even when the web
page is closed. Unsaved drives expire seven days after their newest segment.
The latest drive and actively recording routes are exempt from age expiry.
Below 5 GB or 10% free, unsaved segments can be removed sooner. Saved drives
are excluded from both policies on internal and external storage.

To delete a saved drive, unsave it first, then use **Delete** and its existing
confirmation. Unsaving also makes it eligible for automatic cleanup. Active
recordings cannot be manually deleted. Saved recordings can fill the disk;
the dashboard reports saved bytes and shows a low-space message when needed.

Protection is enforced by the deleter, not by keeping a browser page open.
Save writes and deletion share an OS process lock, and saved metadata is
atomically persisted beside the recordings. Unreadable metadata pauses cleanup
instead of treating protected drives as disposable. No drive data is uploaded
or copied when saving.


## 3.7.9 pressure recovery and incident clips

Check free space every second in deleter, without IPC subscriptions or blocking
control/hardware publishers. Start cleanup below 15% free or 5 GiB, and continue
until at least 20% and 7 GiB are free. Age scans remain at a 60-second cadence
when there is no space pressure. At pressure, deletion drains one segment per
100 ms; saved, recording, and incident-pinned segments remain hard exclusions.
A disk consisting entirely of protected files cannot be automatically reclaimed.

`.fp_feedback_pins.json` shares the save/deletion lock. Its per-report list pins
only the three surrounding minute segments until `clip.json` and hardlinks are
durable. Invalid protection metadata pauses deletion. Web deletion also honors
pins. Explicit saved flags are never changed by report capture or upload.
Interrupted clips resume on restart, and unacknowledged reports are retained.
Unlinking a cloud-acknowledged report link cannot remove a saved original drive.
Legacy automatic saves cannot be safely distinguished from explicit saves; they
are not silently migrated/unsaved.
