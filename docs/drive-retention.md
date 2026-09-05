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
