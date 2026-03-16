#!/bin/bash
# live_simulator.sh
STATIC_MPD="morpheus/stream_with_scte35_events.mpd"
COUNTER=0

while true; do
    COUNTER=$((COUNTER + 1))
    CURRENT_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    
    cat "$STATIC_MPD" | \
    sed "s/type=\"static\"/type=\"dynamic\"/g" | \
    sed "s/mediaPresentationDuration=\"[^\"]*\"//g" | \
    sed "s|<MPD |<MPD availabilityStartTime=\"${CURRENT_TIME}\" publishTime=\"${CURRENT_TIME}\" |" | \
    sed 's|<UTCTiming [^>]*/>|<UTCTiming schemeIdUri="urn:mpeg:dash:utc:http-xsdate:2014" value="https://time.akamai.com/?iso"/>|g' | \
    curl -X PUT http://localhost:8080/live.mpd?mode=scte-to-alternative \
      -H "Content-Type: application/dash+xml" \
      --data-binary @-
    
    echo "Update $COUNTER at $(date)"
    sleep 2
done