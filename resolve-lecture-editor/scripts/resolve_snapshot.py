"""Read-only Resolve MCP script. Pass this file's text to run_script.

The MCP injects resolve/project. This is not a standalone shell script.
Does not transcribe, duplicate, edit, save or render anything.
"""
if project is None:
    raise ValueError("No active Resolve project")
timeline = project.GetCurrentTimeline()
if timeline is None:
    raise ValueError("No active timeline")
tracks = []
for kind in ("video", "audio", "subtitle"):
    for index in range(1, timeline.GetTrackCount(kind) + 1):
        items = []
        for item in timeline.GetItemListInTrack(kind, index) or []:
            media = item.GetMediaPoolItem()
            row = {"id": item.GetUniqueId(), "name": item.GetName(),
                   "in_frame": item.GetStart(), "end_frame_raw": item.GetEnd(),
                   "duration_frames": item.GetDuration(), "enabled": item.GetClipEnabled(),
                   "media_id": media.GetUniqueId() if media else None}
            if kind in ("video", "audio"):
                row.update(source_in=item.GetSourceStartFrame(),
                           source_end_raw=item.GetSourceEndFrame(),
                           properties=item.GetProperties(), speed=item.GetSpeed(),
                           linked_ids=[x.GetUniqueId() for x in item.GetLinkedItems() or []])
            items.append(row)
        tracks.append({"type": kind, "index": index,
                       "name": timeline.GetTrackName(kind, index),
                       "enabled": timeline.GetIsTrackEnabled(kind, index),
                       "locked": timeline.GetIsTrackLocked(kind, index), "items": items})
result = {"project_id": project.GetUniqueId(), "project_name": project.GetName(),
          "timeline_id": timeline.GetUniqueId(), "timeline_name": timeline.GetName(),
          "start_frame": timeline.GetStartFrame(), "end_frame_raw": timeline.GetEndFrame(),
          "start_timecode": timeline.GetStartTimecode(),
          "project_settings": project.GetSettings(), "timeline_settings": timeline.GetSettings(),
          "tracks": tracks, "markers": timeline.GetMarkers()}
