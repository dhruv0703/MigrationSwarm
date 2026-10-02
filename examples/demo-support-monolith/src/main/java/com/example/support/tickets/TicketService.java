package com.example.support.tickets;

import com.example.support.shared.AuditContext;
import com.example.support.shared.NotificationClient;
import com.example.support.users.UserDirectory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class TicketService {
    private final TicketRepository repository;
    private final UserDirectory users;
    private final NotificationClient notifications;
    private final AuditContext audit;

    public TicketService(TicketRepository repository, UserDirectory users,
                         NotificationClient notifications, AuditContext audit) {
        this.repository = repository;
        this.users = users;
        this.notifications = notifications;
        this.audit = audit;
    }

    @Transactional
    public Ticket open(String subject, String requesterId) {
        users.requireActive(requesterId);
        Ticket ticket = repository.save(new Ticket(subject, requesterId));
        notifications.send(requesterId, "Ticket opened: " + subject);
        audit.record("ticket.opened", requesterId);
        return ticket;
    }
}
