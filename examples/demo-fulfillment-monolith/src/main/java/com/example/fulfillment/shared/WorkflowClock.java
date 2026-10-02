package com.example.fulfillment.shared;

import java.time.Clock;

public final class WorkflowClock {
    private WorkflowClock() {
    }

    public static Clock system() {
        return Clock.systemUTC();
    }
}
