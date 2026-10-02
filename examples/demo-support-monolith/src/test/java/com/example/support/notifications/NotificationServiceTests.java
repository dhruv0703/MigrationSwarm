package com.example.support.notifications;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class NotificationServiceTests {
    @Test
    void notificationKeepsRecipient() {
        assertEquals("user", new SupportNotification("user", "hello").getRecipient());
    }
}
